"""what Reedus remembers, per account, in the relay's database.

    bulletin   a short list of settled facts about the person (name, schedule, likes). goes in
               every prompt. the relay is the source of truth; each S3 keeps a copy in flash
               (the `bulletin` message) so a future offline mode still knows who you are.
    history    every turn, searchable (Postgres full-text). the few past moments that match
               what was just said ride along in the prompt as "recalled".

after a conversation ends, one cheap LLM call folds anything new into the bulletin.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import List, Optional, Tuple

from reedus_relay.db import db

MAX_BULLETIN_CHARS = 1500
MAX_BULLETIN_LINES = 24

_UPDATE_SYSTEM = (
    "You keep Reedus's notes about the person he talks to: a short list of durable facts "
    "(their name, people in their life, schedule, school or work, likes and dislikes, plans, "
    "things they asked him to remember). You get the current notes and the latest conversation.\n"
    "Return the complete updated notes: one fact per line, each starting with \"- \". Keep "
    "facts that still hold, add new ones the person stated or clearly confirmed, fix or drop "
    "ones they corrected, merge duplicates. No guesses, no small talk, nothing about Reedus "
    f"himself. At most {MAX_BULLETIN_LINES} lines. If nothing changed, return the notes exactly "
    "as they were. Output only the list."
)


def bulletin(account_id: str) -> Tuple[str, int]:
    row = db().one("SELECT text, rev FROM bulletins WHERE account_id = %s", (account_id,))
    return (row["text"], int(row["rev"])) if row else ("", 0)


def set_bulletin(account_id: str, text: str) -> int:
    text = clean_bulletin(text)
    _, rev = bulletin(account_id)
    d = db()
    if rev:
        d.execute("UPDATE bulletins SET text = %s, rev = %s, updated = %s WHERE account_id = %s",
                  (text, rev + 1, d.now(), account_id))
    else:
        d.execute("INSERT INTO bulletins (account_id, text, rev, updated) VALUES (%s, %s, 1, %s)",
                  (account_id, text, d.now()))
    return rev + 1


def clean_bulletin(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•\d.)\s]+", "", line).strip()
        if line and line.lower() not in {l.lower()[2:] for l in lines}:
            lines.append(f"- {line}")
    out = "\n".join(lines[:MAX_BULLETIN_LINES])
    return out[:MAX_BULLETIN_CHARS].rsplit("\n", 1)[0] if len(out) > MAX_BULLETIN_CHARS else out


def record_turn(account_id: str, device_id: str, conversation: str, said: str, reply: str,
                usd: float) -> None:
    d = db()
    d.execute("INSERT INTO turns (account_id, device_id, conversation, at, said, reply, usd) "
              "VALUES (%s, %s, %s, %s, %s, %s, %s)",
              (account_id, device_id, conversation, d.now(), said[:2000], reply[:4000], float(usd)))


def recall(account_id: str, said: str, conversation: str, limit: int = 3) -> str:
    """Past moments that match what was just said, as a prompt block, or ''."""
    rows = db().search_turns(account_id, said, limit=limit, exclude_conversation=conversation)
    if not rows:
        return ""
    out = []
    for r in rows:
        when = _ago(r["at"])
        out.append(f"- {when}, they said: \"{_clip(r['said'], 160)}\" and you said: \"{_clip(r['reply'], 200)}\"")
    return "\n".join(out)            # persona.py adds the "past conversations" header


def conversation_turns(conversation: str) -> List[dict]:
    return db().all("SELECT said, reply FROM turns WHERE conversation = %s ORDER BY at", (conversation,))


def fold_in(account_id: str, conversation: str, backend) -> Optional[str]:
    """Update the bulletin from one finished conversation. Returns the new text if it changed."""
    turns = conversation_turns(conversation)
    if not turns:
        return None
    current, _ = bulletin(account_id)
    convo = "\n".join(f"Them: {t['said']}\nReedus: {t['reply']}" for t in turns[-20:])
    msgs = [{"role": "system", "content": _UPDATE_SYSTEM},
            {"role": "user", "content": f"Current notes:\n{current or '(none yet)'}\n\n"
                                        f"Latest conversation:\n{convo}"}]
    try:
        out = backend.chat(msgs, 500, use_adapter=False, tier="router", name="bulletin")
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] bulletin update failed: {type(e).__name__}: {e}", flush=True)
        return None
    new = clean_bulletin(out)
    if not new or new == clean_bulletin(current):
        return None
    set_bulletin(account_id, new)
    return new


def forget_all(account_id: str) -> None:
    d = db()
    d.execute("DELETE FROM turns WHERE account_id = %s", (account_id,))
    d.execute("DELETE FROM bulletins WHERE account_id = %s", (account_id,))


def recent(account_id: str, limit: int = 30) -> List[dict]:
    return db().all("SELECT at, said, reply FROM turns WHERE account_id = %s ORDER BY at DESC LIMIT %s",
                    (account_id, int(limit)))


def _clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _ago(at: float) -> str:
    secs = max(0.0, time.time() - float(at))
    if secs < 3600:
        return "earlier today" if secs > 600 else "a few minutes ago"
    if secs < 86400:
        return "earlier today"
    days = int(secs // 86400)
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    return "on " + datetime.fromtimestamp(at).strftime("%b %d")
