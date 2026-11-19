"""one-tap top-ups with Stripe Checkout, and auto top-up from a saved card.

    ReedOS "Top up $10" ──► start(): the relay asks Stripe for a Checkout page
           ◄── url ────────  (card, Apple Pay, Google Pay: one tap on a phone)
    Stripe sends you back to https://<relay>/?topup=<session>#standalone
    ReedOS ──► confirm(): the relay asks Stripe "paid?" and credits the pool, exactly once

no webhook needed (the relay's websocket server doesn't take POSTs): reconcile() re-checks open
sessions every 30 s, so closing the tab before coming back still credits the pool. every session
the relay starts is in the `topups` table; crediting flips `credited` 0 → 1 in one UPDATE, so a
confirm racing the reconciler can't double-credit.

auto top-up: tick "save my card" on a top-up and Checkout keeps the card for later
(setup_future_usage=off_session, under a Stripe Customer for the account; Stripe's page shows
the consent line). after that, whenever a charge leaves the pool under `below`, maybe_auto()
charges the card `amount` off-session. guard rails: one automatic charge per AUTO_GAP_S, at most
AUTO_PER_DAY a day, an Idempotency-Key per attempt so a retry can't charge twice, and any decline
or bank check (3-D Secure) switches auto top-up off and says why in ReedOS.

STRIPE_SECRET_KEY        from Setup in ReedOS, or a Railway variable (sk_/rk_, test or live).
                         a restricted key needs Checkout Sessions, Customers, PaymentIntents and
                         PaymentMethods = Write for auto top-up (Checkout Sessions alone otherwise)
RELAY_PUBLIC_URL         https://<relay>; defaults to https://$RAILWAY_PUBLIC_DOMAIN
RELAY_STRIPE_API         tests point this at a fake Stripe
"""
from __future__ import annotations

import ipaddress
import os
import threading
import time
import urllib.parse
import uuid
from typing import Optional

from reedus_relay import accounts
from reedus_relay.config import env, env_float
from reedus_relay.db import db

AMOUNTS = [5, 10, 20, 50]
AUTO_BELOW = [1, 2, 5]
MIN_USD = env_float("RELAY_TOPUP_MIN", 5.0)
MAX_USD = env_float("RELAY_TOPUP_MAX", 200.0)
OPEN_FOR_S = 24 * 3600
AUTO_GAP_S = env_float("RELAY_AUTO_GAP_S", 600.0)
AUTO_PER_DAY = int(env_float("RELAY_AUTO_PER_DAY", 3))
_auto_locks: dict = {}
_auto_locks_guard = threading.Lock()


class StripeError(RuntimeError):
    def __init__(self, message: str, status: int = 0, code: str = "", decline: str = ""):
        super().__init__(message)
        self.status, self.code, self.decline = status, code, decline


def api() -> str:
    return (env("RELAY_STRIPE_API") or "https://api.stripe.com").rstrip("/")


def enabled() -> bool:
    return bool(os.environ.get("STRIPE_SECRET_KEY"))


def public_base() -> str:
    explicit = env("RELAY_PUBLIC_URL").rstrip("/")
    if explicit:
        return explicit
    dom = env("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{dom}" if dom else ""


def _allowed_return(url: str) -> bool:
    """Where Stripe may send people back: this relay, or a ReedOS on your own network."""
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    base = public_base()
    if base and urllib.parse.urlparse(base).netloc == u.netloc:
        return True
    extra = [o.strip().rstrip("/") for o in env("RELAY_RETURN_ORIGINS").split(",") if o.strip()]
    if f"{u.scheme}://{u.netloc}" in extra:
        return True
    host = u.hostname
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request(method: str, path: str, data=None, key: Optional[str] = None,
             params=None, idempotency: str = "") -> dict:
    import requests
    key = key or os.environ.get("STRIPE_SECRET_KEY") or ""
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency:
        headers["Idempotency-Key"] = idempotency
    r = requests.request(method, api() + path, data=data, params=params, timeout=20, headers=headers)
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        err = body.get("error") or {}
        raise StripeError(f"Stripe: {err.get('message') or f'HTTP {r.status_code}'}", r.status_code,
                          err.get("code") or "", err.get("decline_code") or "")
    return body


def test_key(key: str) -> dict:
    try:
        _request("GET", "/v1/balance", key=key)
    except Exception as e:                                       # noqa: BLE001
        return {"ok": False, "detail": str(e)[:240]}
    mode = "live" if "_live_" in key else "test"
    return {"ok": True, "detail": f"Stripe answered ({mode} mode"
                                  + (", payments are pretend until you switch to a live key)" if mode == "test" else ")")}


# ---------- the account's Stripe side (in accounts.settings["billing"]) ----------
def _billing(acc: dict) -> dict:
    return dict((acc.get("settings") or {}).get("billing") or {})


def _save_billing(aid: str, billing: dict) -> None:
    accounts.set_account_settings(aid, {"billing": billing})


def _customer(acc: dict) -> str:
    b = _billing(acc)
    if b.get("customer"):
        return b["customer"]
    c = _request("POST", "/v1/customers", {"name": acc.get("name") or "Reedus",
                                           "description": "Reedus relay account",
                                           "metadata[account]": acc["id"]},
                 idempotency=f"cust-{acc['id']}")
    b["customer"] = c["id"]
    _save_billing(acc["id"], b)
    return c["id"]


def billing_status(acc: dict) -> dict:
    b = _billing(acc)
    auto = b.get("auto") or {}
    return {"card": b.get("card"), "auto": {"enabled": bool(auto.get("enabled") and b.get("card")),
                                            "below": auto.get("below", 2), "amount": auto.get("amount", 10)},
            "auto_failed": b.get("auto_failed") or "", "amounts": AMOUNTS, "below_options": AUTO_BELOW,
            "limits": {"gap_min": int(AUTO_GAP_S // 60), "per_day": AUTO_PER_DAY}}


def set_auto(acc: dict, enabled_: bool, below=None, amount=None) -> dict:
    b = _billing(acc)
    auto = dict(b.get("auto") or {"below": 2, "amount": 10})
    if below is not None:
        below = float(below)
        if not 0.5 <= below <= 50:
            return {"error": "the threshold is $0.50 to $50"}
        auto["below"] = below
    if amount is not None:
        amount = float(amount)
        if not MIN_USD <= amount <= MAX_USD:
            return {"error": f"auto top-ups are ${MIN_USD:.0f} to ${MAX_USD:.0f}"}
        auto["amount"] = amount
    if enabled_ and not b.get("card"):
        return {"error": "save a card first: tick \"save my card\" on your next top-up"}
    auto["enabled"] = bool(enabled_)
    b["auto"] = auto
    if enabled_:
        b.pop("auto_failed", None)
    _save_billing(acc["id"], b)
    return {"ok": True, **billing_status(accounts.account(acc["id"]))}


def forget_card(acc: dict) -> dict:
    b = _billing(acc)
    card = b.get("card") or {}
    if card.get("pm") and enabled():
        try:
            _request("POST", f"/v1/payment_methods/{urllib.parse.quote(card['pm'])}/detach")
        except StripeError as e:
            if e.status != 404:
                return {"error": str(e)[:240]}
    b.pop("card", None)
    b["auto"] = {**(b.get("auto") or {}), "enabled": False}
    _save_billing(acc["id"], b)
    return {"ok": True, **billing_status(accounts.account(acc["id"]))}


# ---------- Checkout ----------
def start(account: dict, usd: float, return_to: str = "", save_card: bool = False,
          auto: Optional[dict] = None) -> dict:
    """A Checkout page for `usd`. Returns {"url", "session"} or {"error"}."""
    if not enabled():
        return {"error": "top-ups aren't set up on this relay yet (Setup → Payments)"}
    try:
        usd = round(float(usd), 2)
    except (TypeError, ValueError):
        return {"error": "pick an amount"}
    if not MIN_USD <= usd <= MAX_USD:
        return {"error": f"top-ups are ${MIN_USD:.0f} to ${MAX_USD:.0f}"}
    base = return_to.rstrip("/") if return_to and _allowed_return(return_to) else public_base()
    if not base:
        return {"error": "the relay doesn't know its own address: set RELAY_PUBLIC_URL on Railway"}
    data = {
        "mode": "payment",
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(int(round(usd * 100))),
        "line_items[0][price_data][product_data][name]": "Reedus credit",
        "line_items[0][price_data][product_data][description]":
            "Cloud credit for Reedus Lite: thinking, his voice, speech-to-text",
        "client_reference_id": account["id"],
        "metadata[account]": account["id"],
        "success_url": f"{base}/?topup={{CHECKOUT_SESSION_ID}}#standalone",
        "cancel_url": f"{base}/?topup=cancel#standalone",
    }
    try:
        if save_card:
            # keep the card for auto top-up; Checkout shows the "saved for future payments" notice
            data["customer"] = _customer(account)
            data["payment_intent_data[setup_future_usage]"] = "off_session"
            if auto:
                b = _billing(accounts.account(account["id"]))
                b["auto"] = {**(b.get("auto") or {}), "enabled": False,
                             "below": float(auto.get("below", 2)), "amount": float(auto.get("amount", usd))}
                _save_billing(account["id"], b)
        s = _request("POST", "/v1/checkout/sessions", data)
    except Exception as e:                                       # noqa: BLE001
        return {"error": str(e)[:240]}
    d = db()
    d.execute("INSERT INTO topups (session_id, account_id, usd, status, credited, kind, save_card, created) "
              "VALUES (%s, %s, %s, 'open', 0, 'checkout', %s, %s)",
              (s["id"], account["id"], usd, 1 if save_card else 0, d.now()))
    return {"url": s["url"], "session": s["id"], "usd": usd}


def confirm(session_id: str, account_id: Optional[str] = None) -> dict:
    """Ask Stripe whether it's paid; credit the pool once. Safe to call any number of times."""
    row = db().one("SELECT session_id, account_id, usd, status, credited, save_card FROM topups "
                   "WHERE session_id = %s AND kind = 'checkout'", (session_id,))
    if not row or (account_id and row["account_id"] != account_id):
        return {"error": "that payment isn't one this relay started"}
    if int(row["credited"]):
        return {"credited": False, "already": True, "status": "paid",
                "balance": accounts.balance(row["account_id"])}
    try:
        params = [("expand[]", "payment_intent.payment_method")] if int(row["save_card"]) else None
        s = _request("GET", f"/v1/checkout/sessions/{urllib.parse.quote(session_id)}", params=params)
    except Exception as e:                                       # noqa: BLE001
        return {"error": str(e)[:240]}
    if s.get("payment_status") == "paid":
        paid = round(float(s.get("amount_total") or 0) / 100.0, 2) or float(row["usd"])
        n = db().execute("UPDATE topups SET credited = 1, status = 'paid' WHERE session_id = %s "
                         "AND credited = 0", (session_id,))
        card = _remember_card(row["account_id"], s) if int(row["save_card"]) else None
        if n == 1:
            bal = accounts.top_up(row["account_id"], paid, f"Stripe top-up ({session_id[-8:]})")
            return {"credited": True, "usd": paid, "status": "paid", "balance": bal, "card": card}
        return {"credited": False, "already": True, "status": "paid",
                "balance": accounts.balance(row["account_id"])}
    status = s.get("status") or "open"
    if status == "expired":
        db().execute("UPDATE topups SET status = 'expired' WHERE session_id = %s", (session_id,))
    return {"credited": False, "status": status, "balance": accounts.balance(row["account_id"])}


def _remember_card(aid: str, session: dict) -> Optional[dict]:
    """The card from a save-my-card Checkout, and auto top-up switched on."""
    pi = session.get("payment_intent") or {}
    pm = pi.get("payment_method") if isinstance(pi, dict) else None
    if isinstance(pm, str) or not pm:
        pm = {"id": pm} if pm else None
    if not pm or not pm.get("id"):
        return None
    c = pm.get("card") or {}
    card = {"pm": pm["id"], "brand": (c.get("brand") or "card").title(), "last4": c.get("last4") or "",
            "exp": f"{c.get('exp_month', '')}/{str(c.get('exp_year', ''))[-2:]}" if c.get("exp_month") else ""}
    acc = accounts.account(aid)
    b = _billing(acc)
    b["card"] = card
    if session.get("customer"):
        b["customer"] = session["customer"] if isinstance(session["customer"], str) else session["customer"].get("id")
    b["auto"] = {**{"below": 2, "amount": 10}, **(b.get("auto") or {}), "enabled": True}
    b.pop("auto_failed", None)
    _save_billing(aid, b)
    return card


def reconcile() -> int:
    """Credit any paid session nobody came back for. Returns how many were credited."""
    if not enabled():
        return 0
    n = 0
    cutoff = time.time() - OPEN_FOR_S
    for row in db().all("SELECT session_id, created FROM topups WHERE status = 'open' AND credited = 0 "
                        "AND kind = 'checkout'"):
        if float(row["created"]) < cutoff:
            db().execute("UPDATE topups SET status = 'expired' WHERE session_id = %s", (row["session_id"],))
            continue
        out = confirm(row["session_id"])
        if out.get("credited"):
            n += 1
            print(f"[relay] top-up {row['session_id'][-8:]} credited ${out['usd']:.2f}", flush=True)
    return n


# ---------- auto top-up ----------
def _lock(aid: str) -> threading.Lock:
    with _auto_locks_guard:
        return _auto_locks.setdefault(aid, threading.Lock())


def maybe_auto(aid: str) -> Optional[dict]:
    """After a charge: refill from the saved card if the pool is under the line. None = nothing done."""
    if not enabled():
        return None
    with _lock(aid):
        acc = accounts.account(aid)
        if not acc:
            return None
        b = _billing(acc)
        auto, card = b.get("auto") or {}, b.get("card") or {}
        if not (auto.get("enabled") and card.get("pm") and b.get("customer")):
            return None
        bal = accounts.balance(aid)
        if bal >= float(auto.get("below", 2)):
            return None
        now = time.time()
        recent = db().all("SELECT created, status FROM topups WHERE account_id = %s AND kind = 'auto' "
                          "AND created > %s ORDER BY created DESC", (aid, now - 86400))
        tried = [r for r in recent if r["status"] in ("paid", "pending")]
        if tried and now - float(tried[0]["created"]) < AUTO_GAP_S:
            return {"skipped": "too soon after the last automatic top-up"}
        if len(tried) >= AUTO_PER_DAY:
            return {"skipped": f"already {AUTO_PER_DAY} automatic top-ups today"}
        usd = round(float(auto.get("amount", 10)), 2)
        attempt = f"auto-{aid}-{int(now)}-{uuid.uuid4().hex[:8]}"     # also the Idempotency-Key
        d = db()
        d.execute("INSERT INTO topups (session_id, account_id, usd, status, credited, kind, created) "
                  "VALUES (%s, %s, %s, 'pending', 0, 'auto', %s)", (attempt, aid, usd, now))
        label = f"{card.get('brand', 'card')} •••• {card.get('last4', '')}".strip()
        try:
            pi = _request("POST", "/v1/payment_intents", {
                "amount": str(int(round(usd * 100))), "currency": "usd",
                "customer": b["customer"], "payment_method": card["pm"],
                "off_session": "true", "confirm": "true",
                "description": "Reedus credit (auto top-up)",
                "metadata[account]": aid, "metadata[kind]": "auto_topup",
            }, idempotency=attempt)
        except StripeError as e:
            why = {"authentication_required": "your bank wants to confirm it's you",
                   "card_declined": "the card was declined",
                   "expired_card": "the card has expired",
                   "insufficient_funds": "the card has insufficient funds"}.get(e.decline or e.code, str(e))
            d.execute("UPDATE topups SET status = 'failed', detail = %s WHERE session_id = %s", (why[:200], attempt))
            _auto_off(aid, f"Auto top-up stopped: {why}. Switch it back on once that's sorted, or save a different card.")
            print(f"[relay] auto top-up for {aid} failed: {why}", flush=True)
            return {"failed": why}
        except Exception as e:                                   # noqa: BLE001
            # couldn't reach Stripe: let the next charge try again (the idempotency key changes)
            d.execute("UPDATE topups SET status = 'failed', detail = %s WHERE session_id = %s",
                      (f"unreachable: {e}"[:200], attempt))
            return {"failed": "Stripe unreachable"}
        if pi.get("status") != "succeeded":
            d.execute("UPDATE topups SET status = 'failed', detail = %s WHERE session_id = %s",
                      (f"status {pi.get('status')}", attempt))
            _auto_off(aid, "Auto top-up stopped: your bank wants to confirm the payment. Top up by hand "
                           "with \"save my card\" ticked to confirm it, and it switches back on.")
            return {"failed": pi.get("status")}
        n = d.execute("UPDATE topups SET status = 'paid', credited = 1, detail = %s WHERE session_id = %s "
                      "AND credited = 0", (pi.get("id", ""), attempt))
        if n == 1:
            bal = accounts.top_up(aid, usd, f"Auto top-up, {label}")
            print(f"[relay] auto top-up for {aid}: ${usd:.2f} from {label}", flush=True)
            return {"credited": True, "usd": usd, "balance": bal}
        return None


def _auto_off(aid: str, why: str) -> None:
    b = _billing(accounts.account(aid))
    b["auto"] = {**(b.get("auto") or {}), "enabled": False}
    b["auto_failed"] = why
    _save_billing(aid, b)


def after_charge(aid: str) -> None:
    """accounts.charge() calls this on a thread after every spend."""
    try:
        maybe_auto(aid)
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] auto top-up check failed: {type(e).__name__}: {e}", flush=True)
