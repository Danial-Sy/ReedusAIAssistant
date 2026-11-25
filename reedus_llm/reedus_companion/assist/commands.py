"""spoken commands for timers, alarms, reminders, lists and the day, answered without a model.

same idea as connect/intents.py: "set a timer for ten minutes" has one right answer and it
should come back instantly. handle() returns what to say, or None to let the persona answer.
"""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta
from typing import Callable, List, Optional

from reedus_companion.assist import when
from reedus_companion.assist.store import Assistant, SNOOZE_S, list_key

_LEAD = re.compile(
    r"^(?:(?:hey|hi|ok|okay|yo|um|uh|so|alright)\s*,?\s+)*(?:reedus\s*,?\s*)?"
    r"(?:(?:can|could|would|will) you\s+|please\s+|i need you to\s+|i want you to\s+|"
    r"go ahead and\s+|quickly\s+|just\s+)*")
_TAIL = re.compile(r"(?:\s*,?\s*(?:please|for me|thanks|thank you|reedus|real quick|now))+$")

_STOP_RING = re.compile(
    r"^(?:stop(?: it| that| the (?:alarm|timer|reminder|ringing|noise))?|turn (?:it|that|the (?:alarm|timer)) off|"
    r"(?:alarm|timer) off|off|ok(?:ay)?(?: i'?m up)?|i'?m up|i'?m awake|thanks?(?: you)?|dismiss|"
    r"enough|quiet|shush|got it|alright|cancel(?: it| that)?|silence|be quiet|shut it off)$")
_SNOOZE = re.compile(r"\bsnooze\b|\b(?:5|10|9) more minutes\b|\bgive me (?:a few|five|ten) more minutes\b")
_HAS_TIMER = re.compile(r"\btimers?\b")
# words that ask for a timer rather than name one ("put a timer on", "can you do a timer")
_TIMER_ASK = {"put", "throw", "stick", "pop", "give", "get", "do", "add", "run", "need", "want", "i", "me",
              "on", "can", "could", "would", "will", "you", "please", "go", "ahead", "and", "hey", "reedus",
              "set", "start", "make", "create", "begin", "lets", "let's", "let", "us", "just", "quick",
              "okay", "ok", "so", "um", "uh", "yo", "gimme", "i'd", "like", "to", "up", "new", "another"}
_CANCEL = r"(?:cancel|stop|delete|remove|clear|kill|turn off|disable|end|get rid of|switch off|shut off|forget)"
_PAIRS = ("mac and cheese", "salt and pepper", "peanut butter and jelly", "fish and chips",
          "bread and butter", "rock and roll", "half and half", "chips and salsa", "pb and j")
_ACK_SET = ("Okay. ", "Got it. ", "", "")


def clean(text: str) -> str:
    t = when.norm(text)
    t = _LEAD.sub("", t)
    t = _TAIL.sub("", t).strip(" ,")
    return t


def _you(text: str) -> str:
    """'call my mom about my car' -> 'call your mom about your car', for reading back."""
    swaps = [(r"\bi'm\b", "you're"), (r"\bi am\b", "you are"), (r"\bmyself\b", "yourself"),
             (r"\bmy\b", "your"), (r"\bmine\b", "yours"), (r"\bme\b", "you"), (r"\bi\b", "you"),
             (r"\bi've\b", "you've"), (r"\bi'll\b", "you'll")]
    out = text
    for a, b in swaps:
        out = re.sub(a, b, out)
    return out


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _join(parts: List[str], word: str = "and") -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + f" {word} " + parts[-1]


class Commands:
    def __init__(self, store: Assistant, now: Callable[[], datetime] = datetime.now):
        self.a = store
        self.now = now

    # ================= entry =================
    def handle(self, text: str, apps: Optional[Callable[[str], str]] = None) -> Optional[str]:
        self._raw = re.sub(r"\s+", " ", text or "")
        t = clean(text)
        if not t or len(t) > 220:
            return None
        ringing = self.a.ringing()
        if ringing:
            said = self._while_ringing(t, ringing)
            if said is not None:
                return said
        p = self.a.pending
        if p and time.time() - p.get("at", 0) < 90:
            said = self._answer_pending(t, p)
            if said is not None:
                return said
        else:
            self.a.pending = None
        before = self.a.pending
        for fn in (self._lists, self._timer_extend, self._cancel, self._pause_resume,
                   self._queries, self._reminder, self._alarm, self._timer):
            said = fn(t)
            if said is not None:
                break
        else:
            said = self._day(t, apps)
        if said is not None and self.a.pending is before:
            self.a.pending = None             # a new command drops an old unanswered question
        return said

    def _orig(self, piece: str) -> str:
        """`piece` (lowercased by clean()) with the user's own capitals, when it's findable."""
        if not piece:
            return piece
        i = getattr(self, "_raw", "").lower().find(piece)
        return self._raw[i:i + len(piece)] if i >= 0 else piece

    # ================= ringing =================
    def _while_ringing(self, t: str, ringing: List[dict]) -> Optional[str]:
        if _SNOOZE.search(t) and any(i["kind"] in ("alarm", "reminder") for i in ringing):
            mins = when.parse_duration(t)
            secs = mins if mins and 60 <= mins <= 3600 else SNOOZE_S
            self.a.snooze(secs)
            return f"Snoozing for {when.say_duration(secs)}."
        if _STOP_RING.match(t) or re.match(rf"^{_CANCEL}\b", t) and len(t.split()) <= 4:
            gone = self.a.dismiss()
            kinds = {i["kind"] for i in gone}
            if kinds == {"timer"}:
                return random.choice(("Timer stopped.", "Okay, timer's off.", "Done."))
            if "alarm" in kinds:
                return random.choice(("Alarm off.", "Okay, alarm's off.", "Alright, you're up."))
            return "Okay."
        # anything else: you heard it, it stops, and what you said is handled normally
        self.a.dismiss()
        return None

    # ================= follow-ups =================
    def _answer_pending(self, t: str, p: dict) -> Optional[str]:
        kind = p.get("kind")
        if re.match(r"^(?:never ?mind|forget it|cancel|no|nothing|nah)$", t):
            self.a.pending = None
            return "Okay, never mind."
        if kind == "reminder_time":
            due, rep = self._resolve_time(t, prefer="day")
            if due is None:
                return None
            self.a.pending = None
            return self._make_reminder(p["text"], due, rep)
        if kind == "alarm_time":
            due, rep = self._resolve_time("at " + t if re.match(r"^\d", t) else t)
            if due is None:
                return None
            self.a.pending = None
            return self._make_alarm(due, rep, p.get("label", ""))
        if kind == "pick":
            items = [i for i in self.a.active(p["what"]) if i["id"] in p["ids"]]
            if not items:
                self.a.pending = None
                return None
            if re.search(r"\b(?:all|both|every|them all|all of them)\b", t):
                chosen = items
            else:
                chosen = self._pick(t, items, p["what"])
            if not chosen:
                return None
            self.a.pending = None
            return self._do_cancel(chosen, p["what"])
        return None

    def _pick(self, t: str, items: List[dict], what: str) -> List[dict]:
        ords = {"first": 0, "1th": 0, "second": 1, "2th": 1, "third": 2, "3th": 2, "last": -1}
        for w, k in ords.items():
            if re.search(rf"\b(?:the )?{w}(?: one)?\b", t):
                try:
                    return [items[k]]
                except IndexError:
                    return []
        secs = when.parse_duration(t)
        clock = when.parse_clock(t) or when.parse_clock("at " + t)
        label = re.sub(rf"\b(?:the|my|one|{what}s?)\b", " ", t).strip()
        return self.a.find(what, label=label, seconds=secs,
                           clock=(clock[0], clock[1]) if clock else None)

    # ================= timers =================
    def _timer(self, t: str) -> Optional[str]:
        is_timer = _HAS_TIMER.search(t) or re.match(r"^(?:countdown|count down)\b", t)
        if not is_timer:
            return None
        if re.search(r"\b(?:how|what|which|any|left|remaining|status|check|list|show)\b", t):
            return None
        secs = when.parse_duration(t)
        if not secs:
            if re.match(r"^(?:set|start|make|create)\b", t):
                return "How long should the timer be?"
            return None
        if secs > 24 * 3600:
            return "Timers max out at 24 hours. Want an alarm instead?"
        label = self._orig(self._timer_label(t))
        it = self.a.add_timer(secs, label)
        dur = when.say_duration(secs)
        n = len(self.a.active("timer"))
        extra = f" That's {n} timers going." if n > 1 else ""
        if it["label"]:
            return f"{_cap(it['label'])} timer, {dur}, starting now.{extra}"
        return random.choice((f"{_cap(dur)}, starting now.", f"Okay, {dur}. Starting now.",
                              f"Timer set for {dur}.")) + extra

    def _timer_label(self, t: str) -> str:
        dur_rx = when.DURATION
        m = re.search(r"\b(?:called|named|labell?ed|for the|for my)\s+(.+?)(?=\s+for\s+|\s+(?:that|which)\b|$)", t)
        if m and not re.fullmatch(dur_rx + r"(?:\s+from now)?", m.group(1)):
            lab = m.group(1)
        else:
            m = re.search(r"\bfor\s+(?!an?\s|\d|half\b|a couple\b|a few\b|quarter\b)([a-z][a-z' ]{1,30}?)(?=\s+for\s+|$)", t)
            if m and not re.search(dur_rx, m.group(1)):
                lab = m.group(1)
            else:
                pre = re.search(r"(?:^|\b(?:a|an|the|my)\s+)([a-z0-9' ]+?)\s+timers?\b", t)
                lab = pre.group(1) if pre else ""
                # "put a timer on", "I need a timer": the words before "timer" are the ask,
                # not a name. a name comes after the last article ("put the pasta timer on")
                art = re.split(r"\b(?:a|an|the|my)\b", lab)
                lab = art[-1]
                words = lab.split()
                while words and words[0] in _TIMER_ASK:
                    words.pop(0)
                lab = " ".join(words)
                lab = re.sub(rf"^(?:{dur_rx})\s*", "", lab)
                lab = re.sub(r"^(?:minute|second|hour)s?\b", "", lab).strip()
        lab = re.sub(r"\b(?:set|start|make|create|a|an|the|my|new|another|timer|please|up)\b", " ", lab)
        lab = re.sub(r"\s+", " ", lab).strip()
        if re.search(when.DURATION, lab) or len(lab.split()) > 4:
            return ""
        return lab

    def _timer_extend(self, t: str) -> Optional[str]:
        t = re.sub(r"\b(\d+|an?|a few|a couple) more (seconds?|minutes?|hours?)\b", r"\1 \2 more", t)
        m = re.match(rf"^(?:add|put|give (?:it|me)|extend (?:the |my )?(?:(.+?) )?timer by)\s+(?:another\s+)?({when.DURATION})(?:\s+more)?"
                     rf"(?:\s+(?:to|on|onto)\s+(?:the|my)\s+(?:(.+?)\s+)?timer)?$", t)
        if not m:
            m2 = re.match(rf"^(?:add|give it)\s+({when.DURATION})\s+more$", t)
            if not m2:
                return None
            secs, label = when.duration_value(m2.group(1)), ""
        else:
            if not (m.group(3) is not None or "timer" in t or "more" in t or "give it" in t):
                return None
            secs, label = when.duration_value(m.group(2)), (m.group(1) or m.group(3) or "")
        timers = self.a.active("timer")
        if not timers:
            return None if "timer" not in t else "You don't have a timer running."
        hit = self.a.find("timer", label=label) if label else timers[:1]
        if not hit:
            return f"I don't see a {label} timer."
        it = hit[0]
        self.a.extend(it, secs)
        name = f"{it['label']} timer" if it["label"] else "timer"
        return f"Added {when.say_duration(secs)}. Your {name} has {when.say_duration(self.a.left(it), short=True)} left."

    def _pause_resume(self, t: str) -> Optional[str]:
        if not _HAS_TIMER.search(t) and not re.match(r"^(?:pause|resume|unpause) it$", t):
            return None
        pause = re.match(r"^(?:pause|hold|freeze|stop)\b", t) and re.search(r"\bpause|hold|freeze\b", t)
        resume = re.match(r"^(?:resume|unpause|continue|restart|start)\b", t) and not when.parse_duration(t)
        if not (pause or resume):
            return None
        timers = [i for i in self.a.active("timer") if i["state"] == ("active" if pause else "paused")]
        if not timers:
            return "There's no timer to pause." if pause else "There's no paused timer."
        label = re.sub(r"\b(?:pause|hold|freeze|resume|unpause|continue|restart|start|the|my|timers?|it|again|back up)\b", " ", t).strip()
        hit = [i for i in timers if i in self.a.find("timer", label=label)] if label else timers
        hit = hit or timers
        for it in hit:
            (self.a.pause if pause else self.a.resume)(it)
        it = hit[0]
        left = when.say_duration(self.a.left(it), short=True)
        return f"Paused, with {left} left." if pause else f"Resumed. {_cap(left)} to go."

    # ================= cancel (timer/alarm/reminder) =================
    def _cancel(self, t: str) -> Optional[str]:
        m = re.match(rf"^(?:{_CANCEL}|turn my|turn the)\b(.*)$", t)
        if not m:
            return None
        rest = m.group(1)
        kind = "timer" if re.search(r"\btimers?\b", rest) else "alarm" if re.search(r"\balarms?\b", rest) else \
            "reminder" if re.search(r"\breminders?\b", rest) else None
        if kind is None:
            return None
        if t.startswith("turn my") or t.startswith("turn the"):
            if not t.endswith(" off"):
                return None
        items = self.a.active(kind)
        if not items:
            return {"timer": "You don't have any timers running.", "alarm": "You don't have any alarms set.",
                    "reminder": "You don't have any reminders."}[kind]
        if re.search(r"\b(?:all|every|everything)\b", rest) or re.search(rf"\b{kind}s\b", rest) and \
                not re.search(r"\b(?:the|my) \w+ " + kind, rest):
            return self._do_cancel(items, kind)
        desc = re.sub(rf"\b(?:the|my|a|an|{kind}s?|off|please|one|set|for)\b", " ", rest)
        secs = when.parse_duration(desc) if kind == "timer" else None
        clock = when.parse_clock(desc) or when.parse_clock("at " + desc.strip()) if kind == "alarm" else None
        day = when.parse_day(desc, self.now()) if kind == "alarm" else None
        label = desc
        if secs:
            label = re.sub(when.DURATION, " ", label)
        if kind == "reminder":
            label = re.sub(r"\b(?:to|about)\b", " ", label)
        label = re.sub(r"\s+", " ", label).strip()
        if clock or day:
            label = ""
        hit = self.a.find(kind, label=label if not re.fullmatch(r"[\d: ]*(?:am|pm)?", label) else "",
                          seconds=secs, clock=(clock[0], clock[1]) if clock else None)
        if day and kind == "alarm":
            hit = [i for i in (hit or items) if datetime.fromtimestamp(i["due"]).date() == day[0].date()] or hit
        if not hit:
            return f"I couldn't find that {kind}. " + self._list_kind(kind)
        if len(hit) > 1 and len(hit) == len(items) and (label or secs or clock):
            hit = hit                                    # everything matched the description
        elif len(hit) > 1:
            self.a.pending = {"kind": "pick", "what": kind, "ids": [i["id"] for i in hit], "at": time.time()}
            return f"You have {len(hit)} {kind}s: " + _join([self._describe(i) for i in hit]) + ". Which one?"
        return self._do_cancel(hit, kind)

    def _do_cancel(self, items: List[dict], kind: str) -> str:
        self.a.cancel([i["id"] for i in items])
        if len(items) > 1:
            return f"Cancelled all {len(items)} {kind}s."
        it = items[0]
        if kind == "timer":
            return f"{_cap(it['label'])} timer cancelled." if it["label"] else "Timer cancelled."
        return f"Cancelled your {self._describe(it)}."

    def _describe(self, it: dict) -> str:
        now = self.now()
        h24 = bool(self.a.settings.get("glance", {}).get("h24"))
        if it["kind"] == "timer":
            left = when.say_duration(self.a.left(it), short=True)
            name = f"{it['label']} timer" if it["label"] else "timer"
            return f"{name} with {left} left" + (" (paused)" if it["state"] == "paused" else "")
        dt = datetime.fromtimestamp(it["due"])
        if it.get("repeat"):
            at = f"{when.say_clock(dt, h24)} {when.say_days(it['repeat'])}"
        else:
            at = when.say_when(dt, now, h24)
        if it["kind"] == "alarm":
            return f"alarm for {at}" + (f" ({it['label']})" if it.get("label") else "")
        return f"reminder to {it['label']} at {at}"

    def _list_kind(self, kind: str) -> str:
        items = self.a.active(kind)
        if not items:
            return ""
        return "You have: " + _join([self._describe(i) for i in items]) + "."

    # ================= queries =================
    def _queries(self, t: str) -> Optional[str]:
        asks = re.search(r"\b(?:how|what|when|which|any|do i have|list|show|check|tell me|read|left|remaining|status|set for)\b", t)
        if not asks:
            return None
        if re.search(r"\btimers?\b", t) or re.search(r"\bhow (?:much|long) (?:time )?(?:is |'s )?(?:left|remaining)\b|\btime (?:is |'s )?left\b", t):
            timers = self.a.active("timer")
            if not timers:
                return "You don't have any timers running." if "timer" in t else None
            if len(timers) == 1:
                it = timers[0]
                left = when.say_duration(self.a.left(it))
                name = f" on your {it['label']} timer" if it["label"] else ""
                return f"{_cap(left)} left{name}." + (" It's paused." if it["state"] == "paused" else "")
            return f"You have {len(timers)} timers: " + _join([self._describe(i) for i in timers]) + "."
        if re.search(r"\balarms?\b", t) and not re.match(r"^(?:set|make|create)\b", t):
            alarms = self.a.active("alarm")
            if not alarms:
                return "You don't have any alarms set."
            if len(alarms) == 1:
                return f"Your alarm is set for {self._describe(alarms[0]).removeprefix('alarm for ')}."
            return f"You have {len(alarms)} alarms: " + _join([self._describe(i) for i in alarms]) + "."
        if re.search(r"\breminders?\b", t) and not re.match(r"^(?:set|make|create)\b", t):
            rems = self.a.active("reminder")
            day = when.parse_day(t, self.now())
            if day:
                lo = day[0].timestamp()
                rems = [r for r in rems if lo <= r["due"] < lo + 86400]
            if not rems:
                return "You don't have any reminders" + (" then." if day else ".")
            return ("You have " + (f"{len(rems)} reminders: " if len(rems) > 1 else "one reminder: ") +
                    _join([self._describe(i).removeprefix("reminder ") for i in rems[:6]]) + ".")
        return None

    # ================= reminders =================
    def _reminder(self, t: str) -> Optional[str]:
        m = re.match(r"^(?:remind me|set (?:a |me a )?reminder|create (?:a )?reminder|make (?:a )?reminder|"
                     r"don'?t let me forget|add (?:a )?reminder)\b\s*(.*)$", t)
        if not m:
            return None
        body = m.group(1).strip()
        body = re.sub(r"^(?:for me|to remind me)\s+", "", body)
        due, rep, task = self._split_time(body, prefer="day")
        task = re.sub(r"^(?:to|that|about|for|i need to|i have to|i gotta|i should)\s+", "", task).strip(" ,")
        task = re.sub(r"\s+(?:to|that)$", "", task)
        if not task:
            return "What should I remind you about?"
        task = self._orig(task)
        if due is None:
            self.a.pending = {"kind": "reminder_time", "text": task, "at": time.time()}
            return f"When should I remind you to {_you(task)}?"
        return self._make_reminder(task, due, rep)

    def _make_reminder(self, task: str, due: datetime, rep: Optional[List[int]]) -> str:
        now = self.now()
        if due <= now:
            return "That time's already passed. When should I remind you?"
        spoken = _you(task)
        self.a.add_reminder(due.timestamp(), spoken, rep)
        h24 = bool(self.a.settings.get("glance", {}).get("h24"))
        if rep:
            at = f"at {when.say_clock(due, h24)} {when.say_days(rep)}"
        elif getattr(self, "_relative", False):
            at = f"in {when.say_duration((due - now).total_seconds())}"
        else:
            at = f"at {when.say_when(due, now, h24)}"
        return random.choice(_ACK_SET) + f"I'll remind you to {spoken} {at}."

    # ================= alarms =================
    def _alarm(self, t: str) -> Optional[str]:
        m = re.match(r"^(?:(?:set|make|create|add|put|schedule)\s+(?:an?\s+|my\s+|the\s+|another\s+)?(?:new\s+)?alarm|alarm|wake me(?: up)?|get me up)\b\s*(.*)$", t)
        if not m:
            m = re.match(r"^(?:set|make|create)\s+(?:an?\s+)?(.+?)\s+alarm\b\s*(.*)$", t)
            if not m:
                return None
            body = m.group(1) + " " + m.group(2)
        else:
            body = m.group(1)
        label = ""
        lm = re.search(r"\b(?:called|named|labell?ed)\s+(.+)$", body)
        if lm:
            label, body = self._orig(lm.group(1).strip()), body[:lm.start()].strip()
        due, rep, _rest = self._split_time(body, prefer="soonest", alarm=True)
        if due is None:
            self.a.pending = {"kind": "alarm_time", "label": label, "at": time.time()}
            return "What time should I set it for?"
        return self._make_alarm(due, rep, label)

    def _make_alarm(self, due: datetime, rep: Optional[List[int]], label: str = "") -> str:
        now = self.now()
        h24 = bool(self.a.settings.get("glance", {}).get("h24"))
        self.a.add_alarm(due.timestamp(), label, rep)
        if rep:
            return f"Alarm set for {when.say_clock(due, h24)} {when.say_days(rep)}."
        until = due - now
        tail = ""
        if until > timedelta(hours=1):
            hrs, rem = divmod(int(until.total_seconds()) // 60, 60)
            if hrs < 24:
                tail = f" That's {hrs} hour{'s' if hrs != 1 else ''}" + (f" and {rem} minutes" if rem else "") + " from now."
        return f"Alarm set for {when.say_when(due, now, h24)}.{tail}"

    # ================= shared time splitting =================
    def _split_time(self, body: str, prefer: str = "soonest", alarm: bool = False):
        """(datetime|None, repeat days|None, body with the time words removed)."""
        now = self.now()
        self._relative = False
        rest = " " + body + " "
        due: Optional[datetime] = None
        rep = None
        rm = when.parse_repeat(rest)
        if rm:
            rep = rm[0]
            rest = rest[:rm[1][0]] + " " + rest[rm[1][1]:]
        rel = re.search(rf"\b(?:in|for|after)\s+({when.DURATION})(?:\s+from now)?\b|\b({when.DURATION})\s+from now\b", rest)
        if rel and not rep:
            secs = when.duration_value(rel.group(1) or rel.group(2))
            if secs:
                due = now.replace(microsecond=0) + timedelta(seconds=secs)
                rest = rest[:rel.start()] + " " + rest[rel.end():]
                self._relative = True
                return due, None, re.sub(r"\s+", " ", rest).strip()
        day = when.parse_day(rest, now)
        if day:
            rest = rest[:day[1][0]] + " " + rest[day[1][1]:]
        tonight = day is not None and re.search(r"\btonight\b|\bthis (?:evening|afternoon)\b", body)
        clock = when.parse_clock(rest, require_marker=False)
        if clock is None and alarm:
            clock = when.parse_clock(" at " + rest.strip())
        if clock:
            h, mm, mer, span = clock
            rest = rest[:span[0]] + " " + rest[span[1]:]
            rest = re.sub(r"\b(?:at|for|by|around)\s*$", " ", rest[:span[0]]) + rest[span[0]:]
            if mer is None and tonight and h < 12:
                mer = "pm"
            if mer is None and re.search(r"\bthis morning\b|\bin the morning\b", body):
                mer = "am"
            if rep:
                if mer == "24":
                    hh = h
                elif mer == "pm":
                    hh = 12 if h == 12 else h + 12
                elif mer == "am":
                    hh = 0 if h == 12 else h
                else:   # "every day at 5": an alarm means 5am, a reminder means 5pm; 7-11 read as morning
                    hh = h if alarm or 7 <= h <= 11 or h == 12 else h + 12
                due = when.next_on_days(hh, mm, rep, now)
            elif alarm and mer is None and 4 <= h <= 11:
                # alarms are wake-ups: "alarm for 7" means 7 AM (the reply says AM, easy to fix)
                due = when.resolve_clock(h, mm, "am", now, day[0] if day else None)
            else:
                due = when.resolve_clock(h, mm, mer, now, day[0] if day else None,
                                         prefer="day" if day and day[0].date() != now.date() else "soonest")
        elif day:
            part = re.search(r"\b(?:this |in the )?(morning|afternoon|evening|night)\b", body)
            hh = {"morning": 9, "afternoon": 14, "evening": 18, "night": 20}.get(part.group(1) if part else "", 9)
            if tonight and not part:
                hh = 20
            if alarm and not part:
                return None, rep, body
            due = day[0].replace(hour=hh, minute=0)
            if due <= now and day[0].date() == now.date():
                due = now.replace(second=0, microsecond=0) + timedelta(hours=1)
        elif rep:
            part = re.search(r"\b(morning|afternoon|evening|night)\b", body)
            hh = {"morning": 8, "afternoon": 14, "evening": 18, "night": 21}.get(part.group(1) if part else "", 9)
            due = when.next_on_days(hh, 0, rep, now)
        rest = re.sub(r"\b(?:this|in the) (?:morning|afternoon|evening)\b|\btonight\b|\bat night\b", " ", rest)
        rest = re.sub(r"\s+(?:at|on|for|by)\s*$", "", re.sub(r"\s+", " ", rest).strip())
        rest = re.sub(r"^(?:at|on|for|by)\s+", "", rest)
        return due, rep, rest

    def _resolve_time(self, t: str, prefer: str = "soonest"):
        due, rep, _ = self._split_time(t, prefer=prefer, alarm=True)
        return due, rep

    # ================= lists =================
    def _lists(self, t: str) -> Optional[str]:
        m = re.match(r"^(?:add|put|throw|stick|write down|jot down)\s+(.+?)\s+(?:to|on|onto|in)\s+(?:my|the|our)\s+([a-z\- ]*?)\s*list$", t)
        if m:
            things = [self._orig(x) for x in self._split_items(m.group(1))]
            key, added = self.a.list_add(m.group(2) or "shopping", things)
            name = self._list_name(key)
            if not added:
                return f"{_cap(_join(things))} {'is' if len(things) == 1 else 'are'} already on your {name}."
            return f"Added {_join(added)} to your {name}."
        m = re.match(r"^(?:what(?:'s| is| do i have)|read(?: me)?|tell me(?: what'?s)?|check|show me|what'?s)\s+(?:on\s+)?(?:my|the)\s+([a-z\- ]*?)\s*list$", t) or \
            re.match(r"^what(?:'s| is) on (?:my|the) ([a-z\- ]*?)\s*list$", t) or \
            re.match(r"^what (?:do i need|should i get) (?:from|at) the (?:grocery )?(store|grocery store|supermarket)$", t)
        if m:
            name = m.group(1)
            name = "shopping" if name in ("store", "grocery store", "supermarket", "") else name
            key, items = self.a.list_items(name)
            label = self._list_name(key)
            if not items:
                return f"Your {label} is empty."
            if len(items) > 12:
                return f"Your {label} has {len(items)} things: {_join(items[:12])}, and {len(items) - 12} more."
            return f"On your {label}: {_join(items)}."
        m = re.match(r"^(?:remove|delete|take|cross|check|scratch)\s+(.+?)\s+(?:off|from|out of)\s+(?:my|the)\s+([a-z\- ]*?)\s*list$", t)
        if m:
            key, got = self.a.list_remove(m.group(2) or "shopping", m.group(1))
            return f"Took {got} off your {self._list_name(key)}." if got else \
                f"I don't see {m.group(1)} on your {self._list_name(key)}."
        m = re.match(r"^(?:check off|cross off|mark)\s+(.+?)(?:\s+(?:as\s+)?(?:done|complete|completed|finished))?$", t)
        if m and (t.startswith(("check off", "cross off")) or re.search(r"\b(?:done|complete|completed|finished)$", t)):
            for name in list(self.a.lists):
                key, got = self.a.list_remove(name, m.group(1))
                if got:
                    return f"Checked off {got}."
            return None
        m = re.match(r"^(?:clear|empty|wipe|reset|delete everything (?:on|from))\s+(?:out\s+)?(?:my|the)\s+([a-z\- ]*?)\s*list$", t)
        if m:
            key, n = self.a.list_clear(m.group(1) or "shopping")
            return f"Cleared your {self._list_name(key)}." if n else f"Your {self._list_name(key)} was already empty."
        return None

    @staticmethod
    def _list_name(key: str) -> str:
        return {"shopping": "shopping list", "to-do": "to-do list"}.get(key, f"{key} list")

    @staticmethod
    def _split_items(raw: str) -> List[str]:
        s = raw
        keep = {}
        for i, p in enumerate(_PAIRS):
            if p in s:
                tok = f"\x00{i}\x00"
                keep[tok] = p
                s = s.replace(p, tok)
        parts = re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", s)
        out = []
        for p in parts:
            for tok, v in keep.items():
                p = p.replace(tok, v)
            p = re.sub(r"^(?:some|a|an|the)\s+", "", p.strip())
            if p:
                out.append(p)
        return out

    # ================= the day =================
    def _day(self, t: str, apps=None) -> Optional[str]:
        from reedus_companion.assist import briefing
        now = self.now()
        nxt = re.search(r"\b(?:what(?:'s| is)|when(?:'s| is)|where(?:'s| is))\s+(?:my\s+)?next\s+"
                        r"(class|meeting|event|appointment|lecture|lab|thing|period|shift|exam|test)\b", t) or \
            re.search(r"\bwhat(?:'s| is) next on my (?:calendar|schedule|agenda)\b|\bwhat do i have next\b", t)
        if nxt:
            cals = briefing.calendars()
            if not cals.configured():
                if apps is not None:
                    return apps("What is my next calendar event today and when does it start?")
                return "I can't see your calendar yet. Add it in ReedOS under Settings, Connections, Calendars."
            kind = nxt.group(1) if nxt.lastindex else ""
            kind = "" if kind in ("event", "thing", "meeting", "appointment") else kind
            ev = briefing.next_event(now.astimezone(), within_h=7 * 24, kind=kind)
            if ev is None:
                return "Nothing else on your calendar this week."
            h24 = bool(self.a.settings.get("glance", {}).get("h24"))
            nowa = now.astimezone()
            if ev["start"] <= nowa:
                return f"You're in {ev['title']} right now, until {when.say_clock(ev['end'].astimezone().replace(tzinfo=None), h24)}."
            mins = int((ev["start"] - nowa).total_seconds() // 60)
            start = ev["start"].astimezone().replace(tzinfo=None)
            at = when.say_when(start, now, h24)
            soon = f", in {when.say_duration(mins * 60)}" if mins < 120 else ""
            loc = f" in {ev['location'].split(',')[0]}" if ev.get("location") and len(ev["location"]) < 40 else ""
            return f"{ev['title']}{loc}, at {at}{soon}."
        day_ask = re.search(
            r"\bwhat(?:'s| is| does)\s+(?:on\s+)?my\s+(?:day|schedule|agenda|calendar)\b|"
            r"\bhow(?:'s| is| does) my (?:day|schedule|morning|afternoon) look|"
            r"\bwhat do i have (?:going on|on|planned|scheduled|today|tomorrow|this|on)\b|"
            r"\bdo i have anything\b|\bam i busy\b|\bwhat(?:'s| is) (?:going on|happening) (?:today|tomorrow)\b|"
            r"\b(?:my|the) (?:daily |morning |day )?briefing\b|^brief me\b|^(?:give me |read me )?(?:the )?rundown\b|"
            r"\bwhat(?:'s| is) on (?:the |my )?(?:calendar|schedule|agenda)\b|^(?:check|read) my (?:calendar|schedule)\b", t)
        if not day_ask:
            return None
        day = when.parse_day(t, now)
        target = day[0] if day else now.replace(hour=0, minute=0, second=0, microsecond=0)
        week = re.search(r"\bthis week\b", t)
        if week:
            parts = []
            for k in range(7):
                d = target + timedelta(days=k)
                evs = briefing.day_events(d)
                if evs:
                    first = briefing.say_event(evs[0])
                    parts.append(f"{when.WEEKDAYS[d.weekday()].capitalize()}: {len(evs)}, starting with {first}")
            if not briefing.calendars().configured():
                return None if apps is None else apps("Summarize my calendar for the next 7 days.")
            return ("This week: " + "; ".join(parts) + ".") if parts else "Your week looks clear."
        brief = bool(re.search(r"briefing|brief me|rundown", t))
        lead = briefing.greeting(now) + " " if brief else ""
        return lead + briefing.day_summary(target, now, weather=brief or not day, apps=apps)


_cmds: Optional[Commands] = None


def handle(text: str, apps: Optional[Callable[[str], str]] = None) -> Optional[str]:
    """The fast path for core.Reedus.reply(). Never raises."""
    global _cmds
    try:
        if _cmds is None:
            from reedus_companion.assist.store import assistant
            _cmds = Commands(assistant())
        return _cmds.handle(text, apps)
    except Exception as e:                                       # noqa: BLE001
        import os
        import sys
        if os.environ.get("REEDUS_DEBUG"):
            print(f"\n[debug] assist fast path failed: {e!r}", file=sys.stderr)
        return None
