"""the people Reedus knows by voice, per account, and what he knows about each of them.

    a person     a name, their voiceprints (voiceprint.py), and their own notes (the same kind
                 as the bulletin). "say": how to pronounce the name, swapped in for it in
                 everything he speaks (pronounce()): a respelling, or IPA in slashes, which
                 Inworld speaks as written (/ˈdænjəl/). one person per account can be "me": the account holder,
                 whose notes ARE the account's bulletin, so nothing he already knew is lost.
    teaching     the app starts it on a Lite (Session.enroll_start): they talk for ~20 s and
                 each sentence becomes a voiceprint. "auto" voiceprints get added later from
                 turns he was sure about, so he keeps up as their voice (or the room) changes.
    a turn       is recorded with who said it: their id, "" for the account holder (and for
                 everything from before voices existed, and typed chat), "?" for a voice he
                 didn't recognize once people are set up.

with nobody set up, everything behaves exactly as it did before.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from reedus_relay import voiceprint
from reedus_relay.db import db

UNKNOWN = "?"                    # turns.voice_id for a voice he didn't recognize
OWNER = ""                       # ...for the account holder (and old turns, and typed chat)
MAX_PEOPLE = 12
MAX_ENROLL = 40                  # voiceprints from teaching, per person
MAX_AUTO = 20                    # ...and from turns he was sure about (oldest go first)
MAX_NOTES_CHARS = 1500


# ---------- storage ----------
def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    return name[:40]


def _row(r: dict) -> dict:
    d = db()
    samples = r.get("samples")
    try:
        import json
        samples = json.loads(samples or "[]")
        if not isinstance(samples, list):
            samples = []
    except ValueError:
        samples = []
    return {"id": r["id"], "account_id": r["account_id"], "name": r["name"], "me": bool(r["me"]),
            "samples": samples, "model": r.get("model") or "", "notes": r.get("notes") or "",
            "say": r.get("say") or "",
            "notes_rev": int(r.get("notes_rev") or 0), "last_heard": float(r.get("last_heard") or 0),
            "created": float(r.get("created") or 0), "updated": float(r.get("updated") or d.now())}


def people(account_id: str) -> List[dict]:
    rows = db().all("SELECT * FROM voices WHERE account_id = %s ORDER BY created", (account_id,))
    return [_row(r) for r in rows]


def person(voice_id: str) -> Optional[dict]:
    r = db().one("SELECT * FROM voices WHERE id = %s", (voice_id,))
    return _row(r) if r else None


def public(p: dict) -> dict:
    """What the app sees: never the voiceprints themselves."""
    current = [s for s in p["samples"] if s.get("m") == voiceprint.MODEL_ID]
    return {"id": p["id"], "name": p["name"], "me": p["me"],
            "taught": sum(1 for s in current if s.get("k") == "enroll"),
            "learned": sum(1 for s in current if s.get("k") == "auto"),
            "ready": bool(current), "notes": p["notes"], "say": p.get("say", ""),
            "last_heard": p["last_heard"],
            "created": p["created"]}


def add(account_id: str, name: str, me: bool = False) -> dict:
    name = _clean_name(name)
    if not name:
        raise ValueError("give them a name")
    have = people(account_id)
    if len(have) >= MAX_PEOPLE:
        raise ValueError(f"that's the most people ({MAX_PEOPLE}) one account can have")
    if any(p["name"].lower() == name.lower() for p in have):
        raise ValueError(f"there's already someone called {name}")
    d = db()
    vid = "v_" + secrets.token_hex(6)
    now = d.now()
    if me:
        d.execute("UPDATE voices SET me = 0 WHERE account_id = %s", (account_id,))
    d.execute("INSERT INTO voices (id, account_id, name, me, samples, model, notes, notes_rev, "
              "last_heard, created, updated) VALUES (%s, %s, %s, %s, '[]', %s, '', 0, 0, %s, %s)",
              (vid, account_id, name, 1 if me else 0, voiceprint.MODEL_ID, now, now))
    invalidate(account_id)
    return person(vid)


def rename(voice_id: str, name: str) -> None:
    p = person(voice_id)
    name = _clean_name(name)
    if not p or not name:
        raise ValueError("give them a name")
    if any(q["name"].lower() == name.lower() and q["id"] != voice_id for q in people(p["account_id"])):
        raise ValueError(f"there's already someone called {name}")
    db().execute("UPDATE voices SET name = %s, updated = %s WHERE id = %s", (name, db().now(), voice_id))
    invalidate(p["account_id"])


def set_me(voice_id: str, on: bool = True) -> None:
    p = person(voice_id)
    if not p:
        raise ValueError("no such person")
    d = db()
    if on:
        d.execute("UPDATE voices SET me = 0 WHERE account_id = %s", (p["account_id"],))
    d.execute("UPDATE voices SET me = %s, updated = %s WHERE id = %s", (1 if on else 0, d.now(), voice_id))
    invalidate(p["account_id"])


def remove(voice_id: str) -> None:
    """Forget a person: their voiceprints, their notes, and the turns they said."""
    p = person(voice_id)
    if not p:
        return
    d = db()
    d.execute("DELETE FROM turns WHERE voice_id = %s", (voice_id,))
    d.execute("DELETE FROM voices WHERE id = %s", (voice_id,))
    invalidate(p["account_id"])


def set_notes(voice_id: str, text: str) -> int:
    from reedus_relay.memory import clean_bulletin
    p = person(voice_id)
    if not p:
        raise ValueError("no such person")
    text = clean_bulletin(text)[:MAX_NOTES_CHARS]
    rev = p["notes_rev"] + 1
    db().execute("UPDATE voices SET notes = %s, notes_rev = %s, updated = %s WHERE id = %s",
                 (text, rev, db().now(), voice_id))
    invalidate(p["account_id"])
    return rev


MAX_SAY = 80


def clean_say(say: str) -> str:
    """A respelling ("DAN-yul") as typed, or IPA kept in its slashes. '' = say the name as is."""
    say = re.sub(r"\s+", " ", str(say or "")).strip()[:MAX_SAY]
    if say.startswith("/") or say.endswith("/"):
        core = say.strip("/").strip()
        return f"/{core}/" if core else ""
    return say


def set_say(voice_id: str, say: str) -> str:
    p = person(voice_id)
    if not p:
        raise ValueError("no such person")
    say = clean_say(say)
    db().execute("UPDATE voices SET say = %s, updated = %s WHERE id = %s", (say, db().now(), voice_id))
    invalidate(p["account_id"])
    return say


def say_rules(people_list: List[dict]) -> List[tuple]:
    """(compiled name pattern, what to say) for everyone with a pronunciation, longest names
    first so "Mary Ann" wins over "Mary"."""
    out = []
    for p in sorted(people_list, key=lambda p: -len(p.get("name") or "")):
        say, name = (p.get("say") or "").strip(), (p.get("name") or "").strip()
        if say and name and say.lower() != name.lower():
            out.append((re.compile(rf"(?<![\w/]){re.escape(name)}(?![\w])", re.I), say))
    return out


def apply_say(text: str, rules: List[tuple]) -> str:
    for rx, say in rules:
        text = rx.sub(lambda _m, s=say: s, text)
    return text


def pronounce(account_id: str, text: str) -> str:
    """What goes to the voice: the text with each name swapped for how it's said."""
    if not text:
        return text
    try:
        rules = say_rules(known(account_id))
    except Exception:                                            # noqa: BLE001
        return text
    return apply_say(text, rules) if rules else text


def add_samples(voice_id: str, vecs, kind: str = "enroll", reset: bool = False) -> int:
    """Save voiceprints. reset=True starts their voice over (the app's "Teach again")."""
    import json
    p = person(voice_id)
    if not p:
        raise ValueError("no such person")
    now = db().now()
    keep = [s for s in p["samples"] if s.get("m") == voiceprint.MODEL_ID]
    if reset:
        keep = []
    keep += [{"e": voiceprint.encode(v), "k": kind, "m": voiceprint.MODEL_ID, "at": now} for v in vecs]
    enroll = [s for s in keep if s.get("k") == "enroll"][-MAX_ENROLL:]
    auto = [s for s in keep if s.get("k") == "auto"][-MAX_AUTO:]
    keep = enroll + auto
    db().execute("UPDATE voices SET samples = %s, model = %s, updated = %s WHERE id = %s",
                 (json.dumps(keep, separators=(",", ":")), voiceprint.MODEL_ID, now, voice_id))
    invalidate(p["account_id"])
    return len(keep)


def heard(voice_id: str) -> None:
    db().execute("UPDATE voices SET last_heard = %s WHERE id = %s", (db().now(), voice_id))


def forget_notes(account_id: str) -> None:
    """The app's "Forget everything": everyone's notes go with the bulletin. Their voices stay
    (that's how he tells people apart, not something he learned about them)."""
    db().execute("UPDATE voices SET notes = '', notes_rev = notes_rev + 1 WHERE account_id = %s",
                 (account_id,))
    invalidate(account_id)


# ---------- a short cache: the people and their average voiceprints ----------
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()
CACHE_S = 30.0


def invalidate(account_id: str) -> None:
    with _cache_lock:
        _cache.pop(account_id, None)


def known(account_id: str) -> List[dict]:
    """Everyone on the account, each with "vec" (their average voiceprint, or None until
    they've been taught). Read at most every CACHE_S; any change here clears it."""
    with _cache_lock:
        hit = _cache.get(account_id)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
    out = []
    for p in people(account_id):
        vecs = [voiceprint.decode(s.get("e") or "") for s in p["samples"]
                if s.get("m") == voiceprint.MODEL_ID]
        out.append({**p, "vec": voiceprint.average(vecs)})
    with _cache_lock:
        _cache[account_id] = (time.monotonic(), out)
    return out


# ---------- who's talking ----------
@dataclass
class Who:
    people: List[dict] = field(default_factory=list)   # everyone on the account
    voice: Optional[dict] = None                         # who's talking (None: not recognized)
    match: Optional[voiceprint.Match] = None
    typed: bool = False                                  # the website's chat: the account holder

    @property
    def active(self) -> bool:
        """Is anyone set up? With nobody, everything works the way it always did."""
        return any(p.get("vec") is not None for p in self.people)

    @property
    def me(self) -> Optional[dict]:
        return next((p for p in self.people if p.get("me")), None)

    @property
    def owner_is_speaking(self) -> bool:
        """Whose notes and history this turn uses: the account holder's, unless it's someone
        else he recognized. An unrecognized voice counts as the account holder only when the
        account holder hasn't taught him their own voice (then it most likely is them)."""
        if not self.active or self.typed:
            return True
        if self.voice is not None:
            return bool(self.voice.get("me"))
        return self.me is None or self.me.get("vec") is None

    @property
    def turn_id(self) -> str:
        """What turns.voice_id gets."""
        if not self.active or self.typed:
            return OWNER
        if self.voice is not None:
            return OWNER if self.voice.get("me") else self.voice["id"]
        return UNKNOWN

    def history_ids(self) -> List[str]:
        """The turns recall may search: this person's own."""
        if self.owner_is_speaking:
            ids = [OWNER]
            if self.me is not None:
                ids.append(self.me["id"])                       # never written, but harmless
            if self.me is None or self.me.get("vec") is None:
                ids.append(UNKNOWN)
            return ids
        return [self.voice["id"]] if self.voice is not None else [UNKNOWN]

    @property
    def name(self) -> str:
        return self.voice["name"] if self.voice is not None else ""


def owner_name(who: Who, account: dict) -> str:
    me = who.me
    return (me["name"] if me else "") or (account or {}).get("name") or "the owner"


def identify(account_id: str, audio: bytes, sticky: str = "", device: str = "") -> Who:
    """Who said this clip. Never raises: on any trouble it's simply "not recognized"."""
    try:
        everyone = known(account_id)
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] voices: couldn't read the people: {type(e).__name__}: {e}", flush=True)
        return Who()
    who = Who(people=everyone)
    if not who.active:
        return who                   # nobody taught yet: don't even load the model
    if not voiceprint.available():
        return Who()                 # no model: behave as if nobody was set up
    taught = [(p["id"], p["vec"]) for p in everyone if p.get("vec") is not None]
    t0 = time.perf_counter()
    try:
        got = voiceprint.embed(audio)
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] voices: voiceprint failed: {type(e).__name__}: {e}", flush=True)
        got = None
    vec, secs = got if got else (None, voiceprint.speech_only(audio)[1])
    m = voiceprint.decide(vec, secs, taught, sticky=sticky)
    who.match = m
    who.voice = next((p for p in everyone if p["id"] == m.voice_id), None) if m.voice_id else None
    names = {p["id"]: p["name"] for p in everyone}
    others = ", ".join(f"{names.get(k, k)} {v:.2f}" for k, v in
                       sorted(m.scores.items(), key=lambda kv: -kv[1]))
    print(f"[relay] {device or 'Lite'}: voice: {who.name or 'not recognized'} ({m.how}, "
          f"{m.secs:.1f} s of speech, {int((time.perf_counter() - t0) * 1000)} ms"
          f"{'; ' + others if others else ''})", flush=True)
    return who


def after_turn(who: Who) -> None:
    """The turn was real (words came out of it): note when they were heard, and if he was sure
    it was them, learn a little more of their voice."""
    if who.voice is None or who.match is None:
        return
    try:
        heard(who.voice["id"])
        if who.match.learnable and who.match.vec is not None:
            add_samples(who.voice["id"], [who.match.vec], kind="auto")
    except Exception as e:                                       # noqa: BLE001
        print(f"[relay] voices: couldn't update {who.name}: {type(e).__name__}: {e}", flush=True)


def typed_who(account_id: str) -> Who:
    """Typed chat on the website: whoever's signed in, the account holder."""
    try:
        everyone = known(account_id)
    except Exception:                                            # noqa: BLE001
        everyone = []
    who = Who(people=everyone, typed=True)
    who.voice = who.me
    return who


# ---------- the prompt ----------
def notes_for(who: Who, account_id: str) -> str:
    """The notes that go in this turn's prompt: the bulletin for the account holder (and for
    a voice he can't place, when it most likely is them), the person's own otherwise."""
    from reedus_relay import memory
    if who.owner_is_speaking:
        return memory.bulletin(account_id)[0]
    if who.voice is None:
        return ""                # a voice he can't place, and it isn't the account holder's
    return who.voice.get("notes") or ""


def prompt_block(who: Who, account: dict) -> str:
    """Who he's talking to, for the system prompt. '' with nobody set up."""
    if not who.active:
        return ""
    owner = owner_name(who, account)
    taught = [p["name"] for p in who.people if p.get("vec") is not None]
    if who.typed:
        line = (f"{owner} is typing to you in the app (not speaking out loud)." if who.me else "")
        return line
    if who.voice is not None:
        name = who.voice["name"]
        others = [n for n in taught if n != name]
        out = (f"Who's talking: {name}. You know their voice. The notes below are about {name}"
               + (", whose Lite this is." if who.voice.get("me") else "."))
        if others:
            out += f" Other people you know by voice here: {', '.join(others)}."
        out += (" Use their name now and then, like a friend would, not every reply. If they "
                "say they're someone else, believe them.")
        return out
    out = ("You don't recognize this voice: maybe a guest, maybe someone you know but couldn't "
           "make out this time. Don't call them by a name unless they tell you; if they say who "
           "they are, go with it.")
    if who.owner_is_speaking:
        out += (f" It may well be {owner}, whose Lite this is; the notes below are about {owner}, "
                f"so don't bring up anything private from them unless they do.")
    if taught:
        out += f" People you know by voice here: {', '.join(taught)}."
    return out
