"""turn the user correcting reedus into training data and an immediate fix."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


_WRONG = re.compile(
    r"\b(that'?s (?:not right|wrong|incorrect|false)|thats (?:wrong|not right)|"
    r"(?:no|nah|nope),? (?:that'?s|thats|it'?s) (?:wrong|not right|incorrect)|"
    r"you'?re wrong|that'?s not (?:true|him|her|hers|his|theirs)|"
    r"wrong (?:character|characters|person|guy|name|series|show|anime)|"
    r"not the (?:right|correct) (?:character|characters|person|people|one|ones|answer|name)|"
    r"that'?s not (?:his|her|their)|thats not (?:his|her|their)|"
    r"you'?re (?:thinking|confusing) (?:of|it)|got (?:that|it) (?:wrong|backwards)|"
    r"made that up|not a real|no such)\b", re.I)

_VAGUE = re.compile(
    r"\b(say more|tell me more|elaborate|go deeper|be specific|more specific|"
    r"what do you mean|thats vague|that'?s vague|you'?re (?:being )?vague|stop hedging|"
    r"quit hedging|commit to (?:an answer|something)|pick one|just answer|"
    r"you didn'?t answer|thats a non.?answer|waffl|wishy.?washy|give me a real)\b", re.I)

_TONE = re.compile(
    r"\b(too formal|too stiff|sound like a robot|too long|too short|stop being|"
    r"don'?t be so|be nicer|less cringe|too much|calm down|chill out|weird tone|"
    r"stop saying|don'?t say|quit saying)\b", re.I)


class Signal:
    WRONG = "wrong"
    VAGUE = "vague"
    TONE = "tone"


_CLASSIFIER_SYSTEM = """You decide whether a user is COMPLAINING about the reply an assistant
just gave. Almost always they are not.

Reply with ONLY a JSON object: {"verdict": "..."}, one of:

  none   - THE DEFAULT. The user is just talking. Reacting, agreeing, being surprised, asking
           something new, changing the subject, disagreeing about taste, correcting THEMSELVES,
           or volunteering a fact of their own. If the user is not unhappy with the assistant's
           reply, it is none. When unsure, answer none.
  wrong  - the user says the assistant got a FACT wrong: wrong name, wrong character, wrong
           series, gave someone an ability that isn't theirs, or invented something. Usually
           they name the right answer, or express disbelief AT the assistant's claim.
  vague  - the user says the assistant dodged: too vague, hedged, waffled, wouldn't commit,
           gave a non-answer. Includes terse demands for detail or commitment after a
           non-committal reply: "such as", "pick a side", "thats a cop out", "like what".
  tone   - the user tells the assistant to change HOW IT TALKS: too long, too formal, too
           excited, emoji, robotic. The user must be objecting to the assistant's manner.
           Excitement, slang and swearing ABOUT THE TOPIC are never tone, "bro the animation
           on that" is none.

A CORRECTION OFTEN LOOKS LIKE A PLAIN STATEMENT. People rarely say "you're wrong". They just
say the right thing, tersely, sometimes only a word. Compare what the user says against what
the assistant just claimed, if it contradicts or replaces it, that is wrong, no matter how
flatly it is phrased:

    assistant "Gon's father is Netero."   user "ging. netero is the chairman"   -> wrong
    assistant "Tifa uses a katana."       user "she fights with her fists lol"  -> wrong
    assistant "Minato was the Third."     user "fourth"                          -> wrong

But a statement that does NOT contradict the assistant is just conversation:

    assistant "The Sharingan is the Uchiha kekkei genkai."
    user "sharingan literally means copy wheel eye"                              -> none

Tests that decide the hard cases:

  Is the user reacting to the WORLD, or to the ASSISTANT?
    "that fight makes no sense" = reacting to the story        -> none
    "your answer makes no sense" = reacting to the assistant   -> wrong or vague

  Is the user stating a fact, or correcting one?
    "byakugan means white eye" = sharing trivia                -> none
    "no, byakugan means white eye" = fixing the assistant      -> wrong

  Who is being corrected?
    "sorry i meant the manga" = user fixes themselves          -> none
    "you meant the manga" = user fixes the assistant           -> wrong

  A new question is never a complaint.
    "whats the best fight" -> none, even right after a reply.

NOTE ON LENGTH: this prompt was once compressed from ~795 tokens to ~323 to save CPU time.
Holdout precision on `none` fell 96% -> 85% and did NOT recover when the few-shot examples were
restored, the worked contradiction examples above are what carry the accuracy, not the example
count. It runs after the reply is generated, so the tokens are cheap. Do not shrink it again
without re-running scripts/eval_correction_detect.py.
"""

# twelve examples, not eight: eight dropped holdout precision on `none` 96% -> 85%. this runs
# after the reply now so the extra ~700 tokens delay the next prompt, not the answer.
_FEWSHOT = [
    ("Reedus said: Saitama is my favorite, pure unfiltered power.\n"
     "User replied: uh thats not the right characters those are from one punch man",
     '{"verdict": "wrong"}'),
    ("Reedus said: Gaara fought Rock Lee in the final round.\n"
     "User replied: yooo that fight was insane", '{"verdict": "none"}'),
    ("Reedus said: Kakashi in DMS with one Sharingan is the peak version.\n"
     "User replied: DMS kakashi with one sharingan makes no sense dms literally means dual "
     "mangekyo sharingan", '{"verdict": "wrong"}'),
    ("Reedus said: The Chunin exams arc is where it really picks up.\n"
     "User replied: whats the best fight in the whole series", '{"verdict": "none"}'),
    ("Reedus said: I'd say his abilities are pretty versatile overall.\n"
     "User replied: ok but like which ones specifically", '{"verdict": "vague"}'),
    ("Reedus said: Hashirama founded the village with Madara.\n"
     "User replied: byakugan literally translates to white eye btw", '{"verdict": "none"}'),
    ("Reedus said: So you're saying the anime handled it better?\n"
     "User replied: sorry i meant the manga version", '{"verdict": "none"}'),
    ("Reedus said: That is a fascinating and multifaceted question! Let us explore it.\n"
     "User replied: why are you talking like that", '{"verdict": "tone"}'),
    ("Reedus said: Obito's whole plan hinged on the Infinite Tsukuyomi.\n"
     "User replied: honestly that plan makes no sense when you think about it",
     '{"verdict": "none"}'),
    ("Reedus said: Naruto's Rasengan is iconic.\n"
     "User replied: nah sasuke is way cooler tho", '{"verdict": "none"}'),
    ("Reedus said: Tsunade healed him with Creation Rebirth.\n"
     "User replied: wait really? i dont remember that", '{"verdict": "none"}'),
    ("Reedus said: Gon's father is Netero.\n"
     "User replied: ging. netero is the chairman", '{"verdict": "wrong"}'),
]

_VERDICTS = {"wrong": Signal.WRONG, "vague": Signal.VAGUE, "tone": Signal.TONE, "none": None}

LAST_RAW: List[str] = []


def detect_llm(user_msg: str, prev_reply: str, chat_fn) -> Optional[str]:
    """Semantic correction detection, judges intent instead of matching phrasings."""
    if not user_msg or not prev_reply:
        return None
    msgs = [{"role": "system", "content": _CLASSIFIER_SYSTEM}]
    for ex_in, ex_out in _FEWSHOT:
        msgs.append({"role": "user", "content": ex_in})
        msgs.append({"role": "assistant", "content": ex_out})
    msgs.append({"role": "user", "content":
                 f"Reedus said: {prev_reply[:600]}\nUser replied: {user_msg[:400]}"})
    try:
        raw = (chat_fn(msgs, 24) or "").strip()
    except Exception:                                            # noqa: BLE001
        LAST_RAW.append("<exception>")
        return None
    LAST_RAW.append(raw)

    m = re.search(r'"verdict"\s*:\s*"(\w+)"', raw)
    verdict = (m.group(1) if m else "").lower()
    if verdict not in _VERDICTS:
        # strict: "find any verdict word in the output" was 18 false positives out of 23 negatives
        # because the model reasoning aloud mentioned the words. unparseable means None.
        return None
    return _VERDICTS.get(verdict)


_DENIAL = re.compile(
    r"\b(?:never|didn'?t|did not|doesn'?t|does not|wasn'?t|was not|weren'?t|isn'?t|is not|"
    r"aren'?t|hasn'?t|hadn'?t|wouldn'?t|couldn'?t|don'?t)\b", re.I)


_DENIAL_STOP = frozenset("""
i me my you your he him his she her it its they them their we us our that this those these
a an the of to in on at for from with by as and or but so if then than
is are was were be been being am do does did done have has had
never not no dont didnt doesnt wasnt werent isnt arent hasnt hadnt wouldnt couldnt
really actually literally even just still yeah yea nah ok okay well like mean lol lmao tho
though but yeah get what saying say said think thought remember one thing right
""".split())


def _stem(w: str) -> str:
    """Crude suffix fold so 'hands' and 'handed' compare equal."""
    w = w.lower().strip("'’-")
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(text: str) -> set:
    return {_stem(w) for w in re.findall(r"[A-Za-z'’\-]{3,}", text or "")
            if _stem(w) not in _DENIAL_STOP} - {""}


def detect_contradiction(user_msg: str, prev_reply: str, min_overlap: int = 2) -> Optional[str]:
    """A denial whose subject matter is what the assistant just said."""
    if not user_msg or not prev_reply or len(user_msg) > 400:
        return None
    m = _DENIAL.search(user_msg)
    if not m:
        return None
    denied = _content(user_msg[m.end():m.end() + 120])
    if len(denied & _content(prev_reply)) >= min_overlap:
        return Signal.WRONG
    return None


def detect(user_msg: str) -> Optional[str]:
    """Which correction, if any, this user turn is making."""
    if not user_msg or len(user_msg) > 400:
        return None
    if _TONE.search(user_msg):
        return Signal.TONE
    if _WRONG.search(user_msg):
        return Signal.WRONG
    if _VAGUE.search(user_msg):
        return Signal.VAGUE
    return None



_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    signal      TEXT NOT NULL,          -- wrong | vague | tone
    context     TEXT NOT NULL,          -- the prompt/history the bad reply answered
    rejected    TEXT NOT NULL,          -- what Reedus actually said
    user_said   TEXT NOT NULL,          -- the correction, VERBATIM, never treated as fact
    subject     TEXT,                   -- who the turn was about, if known
    attribute   TEXT,                   -- the thing that was misattributed, if known
    verified    INTEGER NOT NULL DEFAULT 0,
    evidence    TEXT,                   -- the CORPUS passage. the only thing trusted as fact
    corrected   TEXT,                   -- the reply that ANSWERED the correction
    fix_ok      INTEGER NOT NULL DEFAULT 0,   -- did that reply survive validation?
    fix_reason  TEXT,                   -- why it didn't, when it didn't
    exported    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS corrections_subject ON corrections(subject);
CREATE INDEX IF NOT EXISTS corrections_verified ON corrections(verified);
"""


class Corrections:
    """Append-only log of user corrections plus the corpus evidence that settles them."""

    def __init__(self, path: Optional[Path] = None, knowledge=None):
        from reedus_companion import config
        self.path = Path(path or (config.MEMORY_DIR / "corrections.sqlite"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.db.commit()
        self.knowledge = knowledge
        self._ix = None

    def _index(self):
        if self._ix is None:
            try:
                from reedus_companion.knowledge.entity_index import EntityIndex
                ix = EntityIndex()
                ix.db
                self._ix = ix
            except Exception:                                    # noqa: BLE001
                self._ix = False
        return self._ix or None

    def log(self, signal: str, context: str, rejected: str, user_said: str,
            subject: str = "", attribute: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO corrections(ts,signal,context,rejected,user_said,subject,attribute) "
            "VALUES(?,?,?,?,?,?,?)",
            (time.time(), signal, context, rejected, user_said, subject, attribute))
        self.db.commit()
        return int(cur.lastrowid)

    def resolve(self, row_id: int, claim: str) -> Optional[str]:
        """Find corpus support for what the user said, and store that."""
        if not self.knowledge:
            return None
        try:
            evidence = self.knowledge.context_for(claim)
        except Exception:                                        # noqa: BLE001
            return None
        if not evidence:
            return None
        self.db.execute("UPDATE corrections SET verified=1, evidence=? WHERE id=?",
                        (evidence, row_id))
        self.db.commit()
        return evidence

    def note_misattribution(self, row_id: int, subject: str, attribute: str) -> None:
        """Record the structured form when the entity index can name what went wrong."""
        self.db.execute("UPDATE corrections SET subject=?, attribute=? WHERE id=?",
                        (subject, attribute, row_id))
        self.db.commit()

    def validate_fix(self, corrected: str, context: str) -> Tuple[bool, str]:
        """Is the reply that answered a correction actually any good?"""
        if not corrected or len(corrected.split()) < 3:
            return False, "empty or trivial"
        ix = self._index()
        if ix is None:
            return False, "no entity index, cannot validate"
        try:
            from reedus_companion.knowledge.entity_index import (drop_subspans, subjects_in)
            from reedus_companion.scripts.eval_grounding import proper_nouns
            names = drop_subspans(proper_nouns(corrected), ix.has_topic)
            invented = [n for n in names if not ix.has_topic(n) and not ix.has_topic_prefix(n)]
            if invented:
                return False, f"invented: {', '.join(invented[:4])}"
            subjects = subjects_in(context, ix) + subjects_in(corrected, ix)
            if subjects:
                bad = [n for n in names
                       if ix.is_ownable(n) and not any(ix.owns(s, n) for s in subjects)]
                if bad:
                    return False, f"misattributed: {', '.join(bad[:4])}"
        except Exception as e:                                   # noqa: BLE001
            return False, f"validation error: {e}"
        return True, "ok"

    def set_corrected(self, row_id: int, corrected: str, context: str = "") -> Tuple[bool, str]:
        ok, reason = self.validate_fix(corrected, context)
        self.db.execute(
            "UPDATE corrections SET corrected=?, fix_ok=?, fix_reason=? WHERE id=?",
            (corrected, 1 if ok else 0, reason, row_id))
        self.db.commit()
        return ok, reason

    def for_subject(self, subject: str, limit: int = 3) -> List[str]:
        """Verified corrections about this subject, as prompt-ready lines."""
        rows = self.db.execute(
            "SELECT attribute, evidence FROM corrections "
            "WHERE verified=1 AND subject=? AND signal=? ORDER BY ts DESC LIMIT ?",
            (subject, Signal.WRONG, limit)).fetchall()
        return [f"- about {subject}: {(ev or '').strip()[:300]}" for _a, ev in rows if ev]

    def pending(self, signal: Optional[str] = None) -> List[sqlite3.Row]:
        q = "SELECT * FROM corrections WHERE exported=0"
        args: Tuple = ()
        if signal:
            q += " AND signal=?"
            args = (signal,)
        self.db.row_factory = sqlite3.Row
        return self.db.execute(q + " ORDER BY ts", args).fetchall()

    def counts(self) -> Dict[str, int]:
        return {s: n for s, n in self.db.execute(
            "SELECT signal, COUNT(*) FROM corrections GROUP BY signal")}



MAX_ARM_SHARE = float(os.environ.get("REEDUS_DPO_MAX_ARM_SHARE", "0.75"))


def export_pairs(store: Corrections, out_path: Path, mark: bool = True) -> Dict[str, int]:
    """Mined corrections -> DPO pairs, same shape dpo_label.py emits."""
    pairs, tone = [], []
    for r in store.pending():
        if r["signal"] == Signal.TONE:
            tone.append({"prompt": r["context"], "rejected": r["rejected"],
                         "note": r["user_said"]})
            continue
        if r["signal"] == Signal.WRONG:
            chosen = r["corrected"] if r["fix_ok"] else None
            if not chosen:
                continue
            pairs.append({"prompt": r["context"], "chosen": chosen,
                          "rejected": r["rejected"], "kind": "A>B",
                          "source": "conversation"})
        elif r["signal"] == Signal.VAGUE:
            chosen = r["corrected"] if r["fix_ok"] else None
            if not chosen:
                continue
            pairs.append({"prompt": r["context"], "chosen": chosen,
                          "rejected": r["rejected"], "kind": "A>C",
                          "source": "conversation"})

    n_ab = sum(1 for p in pairs if p["kind"] == "A>B")
    n_ac = len(pairs) - n_ab
    total = max(1, len(pairs))
    stats = {"A>B": n_ab, "A>C": n_ac, "tone": len(tone)}

    if pairs:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            for p in pairs:
                fh.write(json.dumps(p, ensure_ascii=False) + "\n")
        if tone:
            tp = out_path.with_name(out_path.stem + "_tone.jsonl")
            with open(tp, "w", encoding="utf-8") as fh:
                for t in tone:
                    fh.write(json.dumps(t, ensure_ascii=False) + "\n")
        if mark:
            store.db.execute("UPDATE corrections SET exported=1 WHERE exported=0")
            store.db.commit()

    share = max(n_ab, n_ac) / total
    if share > MAX_ARM_SHARE and len(pairs) >= 20:
        dominant = "A>B" if n_ab > n_ac else "A>C"
        print(f"  WARNING: {dominant} is {share:.0%} of this set.")
        if dominant == "A>B":
            print("  That is the mix that teaches hedging, the exact failure v2 and v3 hit.")
            print("  Mine more vagueness complaints before training, or top up A>C from")
            print("  dpo_label.py rather than shipping this alone.")
        else:
            print("  Trains confidence without teaching accuracy.")
    return stats
