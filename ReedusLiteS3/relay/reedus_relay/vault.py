"""the relay's API keys: set from the website (Settings → Service keys) or Railway variables.

keys saved from the app live in the relay database (table `secrets`) and win over the Railway
variable of the same name. on load they're placed in this process's environment, which is where
all the brain code already reads them (cloud/llm_settings.py, voice/tts_inworld.py), so nothing
downstream needed to change. VERSION bumps on every change so cached clients get rebuilt.

the database is Railway's private Postgres; anyone with the database can read these, the same
as Railway's own variables. only the account owner (or RELAY_ADMIN_TOKEN) can set them, and the
app only ever gets back the last four characters.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, Optional

from reedus_relay.db import db

VERSION = 0
_LOCK = threading.RLock()
_ORIG: Dict[str, Optional[str]] = {}     # the Railway/env value before the app overrode it

SERVICES = {
    "INWORLD_API_KEY": {
        "id": "inworld", "name": "Inworld", "what": "his voice", "required": True,
        "keyUrl": "https://platform.inworld.ai/",
        "billingUrl": "https://platform.inworld.ai/",
        "steps": [
            "Sign up at platform.inworld.ai.",
            "Under Billing, add a card (the free minutes run out quickly).",
            "Open API Keys, create a key, and copy the Base64 version (the long one).",
            "Paste it here and press Save & test.",
        ],
        "billing": "Pay-as-you-go, well under a cent per reply.",
    },
    "STRIPE_SECRET_KEY": {
        "id": "stripe", "name": "Stripe", "what": "one-tap top-ups", "required": False,
        "keyUrl": "https://dashboard.stripe.com/apikeys",
        "billingUrl": "https://dashboard.stripe.com/settings/payouts",
        "steps": [
            "Sign up at dashboard.stripe.com. Start in test mode: nothing real is charged.",
            "Developers → API keys → Create restricted key. Give it Write on Checkout Sessions, "
            "Customers, PaymentIntents and PaymentMethods.",
            "Paste it here. For real payments, finish Stripe's account setup and do the same "
            "with a live key.",
        ],
        "billing": "Stripe keeps about 2.9% + 30¢ per payment; the full amount becomes credit.",
    },
}


def brain_keys() -> Dict[str, str]:
    """env var -> provider id, for every LLM provider that takes a key."""
    from reedus_companion.cloud import providers
    return {providers.get(p)["env"]: p for p in providers.ORDER if p != "custom"}


def known(name: str) -> bool:
    return name in SERVICES or name in brain_keys()


def load() -> None:
    """Database keys -> this process's environment. Called at startup and after every change."""
    global VERSION
    with _LOCK:
        names = set(SERVICES) | set(brain_keys())
        for n in names:
            _ORIG.setdefault(n, os.environ.get(n))
        rows = {r["name"]: r["value"] for r in db().all("SELECT name, value FROM secrets")}
        for n in names:
            if rows.get(n):
                os.environ[n] = rows[n]
            elif _ORIG.get(n):
                os.environ[n] = _ORIG[n]
            else:
                os.environ.pop(n, None)
        VERSION += 1


def set_key(name: str, value: str) -> None:
    if not known(name):
        raise ValueError(f"unknown key {name!r}")
    value = (value or "").strip()
    d = db()
    d.execute("DELETE FROM secrets WHERE name = %s", (name,))
    if value:
        d.execute("INSERT INTO secrets (name, value, updated) VALUES (%s, %s, %s)", (name, value, d.now()))
    load()


def source(name: str) -> str:
    if db().one("SELECT name FROM secrets WHERE name = %s", (name,)):
        return "app"
    return "railway" if _ORIG.get(name) else ""


def hint(value: Optional[str]) -> str:
    v = (value or "").strip()
    return "" if not v else ("•" * len(v) if len(v) <= 10 else f"{v[:4]}…{v[-4:]}")


def describe(name: str) -> dict:
    v = os.environ.get(name) or ""
    return {"name": name, "set": bool(v), "hint": hint(v), "source": source(name) if v else ""}


def stripe_mode() -> str:
    k = os.environ.get("STRIPE_SECRET_KEY") or ""
    if not k:
        return ""
    return "live" if "_live_" in k else "test"


# ---------- testing a key before it's trusted ----------
def test(name: str, value: Optional[str] = None) -> dict:
    """{"ok", "detail"} for a candidate key (or the saved one when value is None)."""
    value = (value if value is not None else os.environ.get(name) or "").strip()
    if not value:
        return {"ok": False, "detail": "paste a key first"}
    brain = brain_keys()
    if name in brain:
        from reedus_companion.cloud import llm
        out = llm.test(brain[name], key=value)
        return {"ok": bool(out.get("ok")),
                "detail": (f"{out.get('model', 'it')} answered \"{out.get('reply', '')}\" in {out.get('ms')} ms"
                           if out.get("ok") else out.get("error") or "that didn't work")}
    if name == "INWORLD_API_KEY":
        return _test_inworld(value)
    if name == "STRIPE_SECRET_KEY":
        from reedus_relay import funding
        return funding.test_key(value)
    return {"ok": False, "detail": f"unknown key {name!r}"}


def _test_inworld(value: str) -> dict:
    # InworldTTS reads its key from the environment; swap it in just for this check
    with _LOCK:
        old = os.environ.get("INWORLD_API_KEY")
        os.environ["INWORLD_API_KEY"] = value
        try:
            from reedus_companion.voice.tts_inworld import InworldTTS
            audio = InworldTTS().synth("Ready.")
            ok = getattr(audio, "size", 0) > 0
            return {"ok": ok, "detail": "his voice works" if ok else "Inworld answered without audio"}
        except Exception as e:                                   # noqa: BLE001
            msg = str(e)
            if "401" in msg or "403" in msg:
                msg = "Inworld rejected the key. Copy the Base64 version of it, whole."
            elif "402" in msg or "quota" in msg.lower() or "credit" in msg.lower():
                msg = "Inworld says the account needs billing. Add a card in Inworld's Billing page."
            return {"ok": False, "detail": msg[:240]}
        finally:
            if old is None:
                os.environ.pop("INWORLD_API_KEY", None)
            else:
                os.environ["INWORLD_API_KEY"] = old
