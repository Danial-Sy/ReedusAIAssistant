"""accounts, devices and the shared pool of funds.

an account is a person (or household): one balance that every cloud cost comes out of (LLM,
speech-to-text, Inworld, web searches), one memory, one ReedOS sign-in. devices belong to an
account; each S3 has its own token so a lost Lite can be revoked without touching the rest.

tokens are random, shown once, stored only as sha256 hashes.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from typing import Optional

from reedus_relay.db import db


def _hash(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def new_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(24)}"


# ---------- accounts ----------
def create_account(name: str, balance_usd: float = 0.0, owner: Optional[bool] = None) -> dict:
    """Returns the account and its app token (the only time the token is visible).
    The first account on a relay is its owner: it can set the relay's keys from ReedOS."""
    d = db()
    aid = uuid.uuid4().hex[:12]
    token = new_token("rla")
    if owner is None:
        owner = d.one("SELECT id FROM accounts LIMIT 1") is None
    d.execute("INSERT INTO accounts (id, name, token_hash, balance_usd, spent_usd, settings, created) "
              "VALUES (%s, %s, %s, %s, 0, %s, %s)",
              (aid, name.strip() or "Reedus", _hash(token), float(balance_usd),
               d.dumps({"owner": True} if owner else {}), d.now()))
    if balance_usd:
        d.execute("INSERT INTO ledger (account_id, at, usd, what) VALUES (%s, %s, %s, %s)",
                  (aid, d.now(), float(balance_usd), "opening balance"))
    return {"account": account(aid), "token": token}


def account(aid: str) -> Optional[dict]:
    row = db().one("SELECT id, name, balance_usd, spent_usd, settings, created FROM accounts "
                   "WHERE id = %s", (aid,))
    if row:
        row["settings"] = db().loads(row["settings"])
    return row


def is_owner(acc: Optional[dict]) -> bool:
    return bool(acc and (acc.get("settings") or {}).get("owner"))


def account_for_token(token: str) -> Optional[dict]:
    row = db().one("SELECT id, token_hash FROM accounts WHERE token_hash = %s", (_hash(token),))
    if row and hmac.compare_digest(row["token_hash"], _hash(token)):
        return account(row["id"])
    return None


def set_account_settings(aid: str, patch: dict) -> dict:
    acc = account(aid) or {}
    s = {**(acc.get("settings") or {}), **(patch or {})}
    s = {k: v for k, v in s.items() if v is not None}
    db().execute("UPDATE accounts SET settings = %s WHERE id = %s", (db().dumps(s), aid))
    return s


# ---------- devices ----------
def create_device(aid: str, name: str = "Reedus Lite") -> dict:
    d = db()
    if not account(aid):
        raise ValueError(f"no account {aid!r}")
    did = uuid.uuid4().hex[:12]
    token = new_token("rld")
    d.execute("INSERT INTO devices (id, account_id, name, token_hash, settings, assist, last_seen, created) "
              "VALUES (%s, %s, %s, %s, '{}', '{}', 0, %s)",
              (did, aid, name.strip() or "Reedus Lite", _hash(token), d.now()))
    return {"device": device(did), "token": token}


def device(did: str) -> Optional[dict]:
    row = db().one("SELECT id, account_id, name, settings, last_seen, created FROM devices "
                   "WHERE id = %s", (did,))
    if row:
        row["settings"] = db().loads(row["settings"])
    return row


def device_for_token(token: str) -> Optional[dict]:
    row = db().one("SELECT id, token_hash FROM devices WHERE token_hash = %s", (_hash(token),))
    if row and hmac.compare_digest(row["token_hash"], _hash(token)):
        return device(row["id"])
    return None


def devices(aid: str) -> list:
    rows = db().all("SELECT id, name, settings, last_seen FROM devices WHERE account_id = %s "
                    "ORDER BY created", (aid,))
    for r in rows:
        r["settings"] = db().loads(r["settings"])
    return rows


def set_device_settings(did: str, patch: dict) -> dict:
    dev = device(did) or {}
    s = {**(dev.get("settings") or {}), **(patch or {})}
    db().execute("UPDATE devices SET settings = %s WHERE id = %s", (db().dumps(s), did))
    return s


def touch(did: str) -> None:
    db().execute("UPDATE devices SET last_seen = %s WHERE id = %s", (db().now(), did))


def revoke_device(did: str) -> None:
    db().execute("UPDATE devices SET token_hash = %s WHERE id = %s", (_hash(secrets.token_hex(32)), did))


# ---------- the pool ----------
def balance(aid: str) -> float:
    row = db().one("SELECT balance_usd FROM accounts WHERE id = %s", (aid,))
    return float(row["balance_usd"]) if row else 0.0


def top_up(aid: str, usd: float, what: str = "top-up") -> float:
    if usd <= 0:
        raise ValueError("top-up must be positive")
    d = db()
    d.execute("UPDATE accounts SET balance_usd = balance_usd + %s WHERE id = %s", (float(usd), aid))
    d.execute("INSERT INTO ledger (account_id, at, usd, what) VALUES (%s, %s, %s, %s)",
              (aid, d.now(), float(usd), what))
    return balance(aid)


def charge(aid: str, usd: float, what: str) -> float:
    """Take spend out of the pool (it may go slightly negative mid-turn). Returns the balance."""
    if usd <= 0:
        return balance(aid)
    d = db()
    d.execute("UPDATE accounts SET balance_usd = balance_usd - %s, spent_usd = spent_usd + %s "
              "WHERE id = %s", (float(usd), float(usd), aid))
    d.execute("INSERT INTO ledger (account_id, at, usd, what) VALUES (%s, %s, %s, %s)",
              (aid, d.now(), -float(usd), what[:120]))
    # auto top-up: refill from the saved card if this left the pool under the line (funding.py)
    import threading
    from reedus_relay import funding
    threading.Thread(target=funding.after_charge, args=(aid,), name="auto-topup", daemon=True).start()
    return balance(aid)


def funded(aid: str) -> bool:
    return balance(aid) > 0.0


def ledger(aid: str, limit: int = 50) -> list:
    return db().all("SELECT at, usd, what FROM ledger WHERE account_id = %s ORDER BY at DESC, id DESC "
                    "LIMIT %s", (aid, int(limit)))
