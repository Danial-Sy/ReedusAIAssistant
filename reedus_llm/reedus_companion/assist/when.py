"""spoken time -> numbers. durations ("an hour and a half"), clock times ("quarter past 7",
"six thirty tonight"), days ("tomorrow", "on friday"), repeats ("every weekday").

pure functions, no clock reads except through the `now` argument, so tests can pin time.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# ---------- number words -> digits ----------
_ONES = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
         "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
         "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_ORD = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
        "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
        "fourteenth": 14, "fifteenth": 15, "sixteenth": 16, "seventeenth": 17,
        "eighteenth": 18, "nineteenth": 19, "twentieth": 20, "thirtieth": 30}


def words_to_digits(text: str) -> str:
    """'twenty five minutes' -> '25 minutes', 'seven fifteen' -> '7 15', 'ten oh five' -> '10 05'.

    Only tens+ones and 'hundred' combine, so a clock time read as two numbers stays two.
    'second' is left alone (it's a unit far more often than an ordinal here)."""
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", (text or "").lower())
    toks = re.findall(r"[a-z]+|\d+(?:\.\d+)?|[^\sa-z\d]+|\s+", t)
    out: List[str] = []
    i = 0
    while i < len(toks):
        w = toks[i]
        if w in _TENS or (w in _ONES and w != "oh"):
            val = _TENS.get(w, _ONES.get(w))
            j = i + 1
            # tens + ones: "twenty five"
            if w in _TENS and j + 1 < len(toks) and toks[j].isspace() and \
                    toks[j + 1] in _ONES and 0 < _ONES[toks[j + 1]] < 10:
                val += _ONES[toks[j + 1]]
                j += 2
            # "one hundred (and) twenty"
            if j + 1 < len(toks) and toks[j].isspace() and toks[j + 1] == "hundred":
                val *= 100
                j += 2
                if j + 3 < len(toks) and toks[j + 1] == "and" and (toks[j + 3] in _TENS or toks[j + 3] in _ONES):
                    j += 2
                if j + 1 < len(toks) and toks[j].isspace() and (toks[j + 1] in _TENS or toks[j + 1] in _ONES):
                    rest = _TENS.get(toks[j + 1], _ONES.get(toks[j + 1], 0))
                    j += 2
                    if toks[j - 1] in _TENS and j + 1 < len(toks) and toks[j].isspace() and \
                            toks[j + 1] in _ONES and 0 < _ONES[toks[j + 1]] < 10:
                        rest += _ONES[toks[j + 1]]
                        j += 2
                    val += rest
            # "ten oh five" -> 10 05
            if j + 3 < len(toks) and toks[j].isspace() and toks[j + 1] == "oh" and \
                    toks[j + 2].isspace() and toks[j + 3] in _ONES and _ONES[toks[j + 3]] < 10:
                out.append(f"{val} 0{_ONES[toks[j + 3]]}")
                i = j + 4
                continue
            out.append(str(val))
            i = j
            continue
        if w in _ORD:
            out.append(f"{_ORD[w]}th")
            i += 1
            continue
        out.append(w)
        i += 1
    return "".join(out)


def norm(text: str) -> str:
    """Lowercase, digits for number words, single spaces, no trailing punctuation."""
    t = words_to_digits(text)
    t = re.sub(r"\b(\d+)(st|nd|rd|th)\b", r"\1th", t)
    t = re.sub(r"[“”\"]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t.rstrip(".!? ")


# ---------- durations ----------
_UNIT = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
         "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "d": 86400, "day": 86400, "days": 86400}
_UNITS = r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|s|m|h)"
_NUM = r"(?:\d+(?:\.\d+)?|an?|a couple(?: of)?|a few|couple(?: of)?|few)"
_PIECE = rf"(?:{_NUM})(?:\s+and\s+a\s+half)?\s*{_UNITS}\b(?:\s+and\s+a\s+half)?"
DURATION = rf"(?:half\s+an?\s+(?:hour|minute)|(?:a\s+)?quarter\s+(?:of\s+)?an?\s+hour|{_PIECE}(?:(?:\s*,\s*|\s+and\s+|\s+){_PIECE})*)"
_DURATION_RX = re.compile(DURATION)


def _num(s: str) -> float:
    s = s.strip()
    if s in ("a", "an"):
        return 1.0
    if "couple" in s:
        return 2.0
    if "few" in s:
        return 3.0
    return float(s)


def parse_duration(text: str) -> Optional[int]:
    """Seconds for the first duration in already-normalized text, or None."""
    m = _DURATION_RX.search(text)
    if not m:
        return None
    return duration_value(m.group(0))


def duration_value(phrase: str) -> Optional[int]:
    p = phrase.strip()
    if re.match(r"half\s+an?\s+hour", p):
        return 1800
    if re.match(r"half\s+an?\s+minute", p):
        return 30
    if re.match(r"(?:a\s+)?quarter", p):
        return 900
    total = 0.0
    for m in re.finditer(rf"({_NUM})(\s+and\s+a\s+half)?\s*({_UNITS})\b(\s+and\s+a\s+half)?", p):
        n, unit, half = _num(m.group(1)), _UNIT[m.group(3)], m.group(2) or m.group(4)
        total += n * unit + (unit / 2 if half else 0)
    return int(round(total)) if total > 0 else None


def say_duration(sec: float, short: bool = False) -> str:
    """3725 -> '1 hour, 2 minutes and 5 seconds'. short=True drops seconds past 10 minutes."""
    s = int(round(max(0.0, sec)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if short and (h or m >= 10):
        if s >= 30:
            m += 1
            if m == 60:
                h, m = h + 1, 0
        s = 0
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s or not parts:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# ---------- clock times ----------
def _mer(name):
    return (rf"(?P<{name}>a\.?\s?m\.?|p\.?\s?m\.?|in the morning|in the afternoon|in the evening|"
        r"at night|tonight|this morning|this afternoon|this evening|o'?clock)")


CLOCK = (rf"(?:(?P<named>noon|midday|midnight)"
         rf"|(?P<rel>half|quarter|\d{{1,2}})\s+(?P<dir>past|after|to|till|til|before)\s+(?P<rh>\d{{1,2}}|noon|midnight)(?:\s*{_mer('mer1')})?"
         rf"|(?P<h>\d{{1,2}})(?:(?::|\s|\.)(?P<m>\d{{2}}))?(?:\s*{_mer('mer2')})?)")
_CLOCK_RX = re.compile(r"\b" + CLOCK + r"\b")


def _mer_kind(mer: Optional[str]) -> Optional[str]:
    if not mer:
        return None
    m = mer.replace(" ", "").replace(".", "")
    if m.startswith("a") or "morning" in m:
        return "am"
    if m.startswith("p") or any(k in m for k in ("afternoon", "evening", "night", "tonight")):
        return "pm"
    return None                                  # o'clock


def parse_clock(text: str, require_marker: bool = False) -> Optional[Tuple[int, int, Optional[str], Tuple[int, int]]]:
    """(hour 0-23 or 1-12, minute, 'am'|'pm'|None if ambiguous, span) for the first clock time.

    A bare number counts only with "at"/"for" in front or a marker after it ("7 pm", "7:30"),
    so "5 minutes" and "add 2 eggs" never read as times."""
    for m in _CLOCK_RX.finditer(text):
        g = m.groupdict()
        if g.get("named"):
            n = g["named"]
            return (12, 0, "pm", m.span()) if n in ("noon", "midday") else (0, 0, "am", m.span())
        if g.get("rel"):
            rh = g["rh"]
            if rh == "noon":
                base, mer = 12, "pm"
            elif rh == "midnight":
                base, mer = 0, "am"
            else:
                base, mer = int(rh), _mer_kind(g.get("mer1"))
                if not 1 <= base <= 12:
                    continue
            rel = g["rel"]
            mins = 30 if rel == "half" else 15 if rel == "quarter" else int(rel)
            if mins >= 60:
                continue
            if g["dir"] in ("to", "till", "til", "before"):
                total = (base * 60 - mins) % (24 * 60)
                h, mm = divmod(total, 60)
                if mer is None and h == 0:
                    h = 12
            else:
                h, mm = base, mins
            return h, mm, mer, m.span()
        h = int(g["h"])
        mm = int(g["m"]) if g.get("m") else 0
        mer = _mer_kind(g.get("mer2"))
        explicit = bool(g.get("m") or g.get("mer2"))
        before = text[max(0, m.start() - 6):m.start()]
        after = text[m.end():m.end() + 10]
        if not explicit:
            if require_marker or not re.search(r"\b(?:at|for|by|around|till|until)\s+$", before):
                continue
        if re.match(rf"\s*{_UNITS}\b", after) or re.match(r"\s*(?:percent|%|degrees?)", after):
            continue                                 # "5 minutes", "at 50 percent"
        if mm >= 60 or h > 23:
            continue
        if h > 12 or h == 0:
            mer = None
            return h, mm, "24", m.span()
        return h, mm, mer, m.span()
    return None


def resolve_clock(h: int, mm: int, mer: Optional[str], now: datetime,
                  day: Optional[datetime] = None, prefer: str = "soonest") -> datetime:
    """The next datetime for a spoken clock time. Ambiguous hours pick the sooner of am/pm
    (or the daytime reading when prefer='day', for reminders on a future day)."""
    if mer == "24":
        cands = [h]
    elif mer == "am":
        cands = [0 if h == 12 else h]
    elif mer == "pm":
        cands = [12 if h == 12 else h + 12]
    else:
        am, pm = (0 if h == 12 else h), (12 if h == 12 else h + 12)
        cands = [am, pm]
    base = (day or now).replace(second=0, microsecond=0)
    if day is not None:
        if len(cands) == 2 and prefer == "day":
            # 7 -> 7am, 3 -> 3pm, 11 -> 11am: the hour you'd plausibly mean during a day
            hh = cands[0] if 7 <= cands[0] <= 11 else cands[1]
            return base.replace(hour=hh, minute=mm)
        opts = [base.replace(hour=c, minute=mm) for c in cands]
        fut = [o for o in opts if o > now]
        return min(fut) if fut else opts[0]
    opts = []
    for c in cands:
        dt = base.replace(hour=c, minute=mm)
        if dt <= now:
            dt += timedelta(days=1)
        opts.append(dt)
    return min(opts)


# ---------- days ----------
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_WD = r"(?:mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _wd_index(w: str) -> int:
    return next(i for i, d in enumerate(WEEKDAYS) if d.startswith(w[:3]))


def parse_day(text: str, now: datetime) -> Optional[Tuple[datetime, Tuple[int, int]]]:
    """(midnight of the named day, span) for today/tonight/tomorrow/<weekday>/<month day>."""
    m = re.search(r"\b(?:the\s+)?day\s+after\s+tomorrow\b", text)
    if m:
        return _midnight(now) + timedelta(days=2), m.span()
    m = re.search(r"\b(tomorrow|tmrw|tonight|today|this (?:morning|afternoon|evening))\b", text)
    if m:
        d = _midnight(now) + timedelta(days=1 if m.group(1) in ("tomorrow", "tmrw") else 0)
        return d, m.span()
    m = re.search(rf"\b(?:on\s+|this\s+|next\s+|coming\s+)?({_WD})s?\b", text)
    if m:
        ahead = (_wd_index(m.group(1)) - now.weekday()) % 7
        if ahead == 0 and m.group(0).startswith("next"):
            ahead = 7                       # "next friday" said on a friday
        return _midnight(now) + timedelta(days=ahead), m.span()
    m = re.search(rf"\b(?:on\s+)?(?:({_MON})\s+(\d{{1,2}})(?:th)?|(?:the\s+)?(\d{{1,2}})th(?:\s+of\s+({_MON}))?)\b", text)
    if m:
        mon_s = m.group(1) or m.group(4)
        day_n = int(m.group(2) or m.group(3))
        month = next((i + 1 for i, n in enumerate(_MONTHS) if mon_s and n.startswith(mon_s[:3])), now.month)
        try:
            d = _midnight(now).replace(month=month, day=day_n)
        except ValueError:
            return None
        if d.date() < now.date():
            if mon_s:
                d = d.replace(year=d.year + 1)
            else:
                nm = month % 12 + 1
                try:
                    d = d.replace(year=d.year + (month == 12), month=nm)
                except ValueError:
                    return None
        return d, m.span()
    return None


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- repeats ----------
def parse_repeat(text: str) -> Optional[Tuple[List[int], Tuple[int, int]]]:
    """(weekday numbers Mon=0, span) for every day / weekdays / weekends / every monday…"""
    m = re.search(r"\b(?:every\s*day|everyday|daily|each day|every (?:morning|night|evening|afternoon))\b", text)
    if m:
        return list(range(7)), m.span()
    m = re.search(r"\b(?:every\s+|on\s+)?week\s?days\b|\bevery\s+week\s?day\b|\bmonday (?:through|to|thru) friday\b", text)
    if m:
        return [0, 1, 2, 3, 4], m.span()
    m = re.search(r"\b(?:every\s+|on\s+)?week\s?ends\b|\bevery\s+weekend\b", text)
    if m:
        return [5, 6], m.span()
    m = re.search(rf"\b(?:every\s+({_WD})|(?:on\s+)?({_WD})s)\b((?:(?:\s*,\s*|\s+and\s+|\s+&\s+)(?:every\s+)?{_WD}s?)*)", text)
    if m:
        days = [_wd_index(m.group(1) or m.group(2))]
        for w in re.findall(_WD, m.group(3) or ""):
            days.append(_wd_index(w))
        return sorted(set(days)), m.span()
    return None


def next_on_days(h: int, mm: int, days: List[int], now: datetime) -> datetime:
    """Next time h:mm lands on one of `days` (Mon=0), strictly after now."""
    base = now.replace(hour=h, minute=mm, second=0, microsecond=0)
    for k in range(8):
        cand = base + timedelta(days=k)
        if cand.weekday() in days and cand > now:
            return cand
    return base + timedelta(days=7)


def say_days(days: List[int]) -> str:
    d = sorted(set(days))
    if d == list(range(7)):
        return "every day"
    if d == [0, 1, 2, 3, 4]:
        return "every weekday"
    if d == [5, 6]:
        return "every weekend"
    names = [WEEKDAYS[i].capitalize() + "s" for i in d]
    return "on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


# ---------- speaking times back ----------
def say_clock(dt: datetime, h24: bool = False) -> str:
    if h24:
        return dt.strftime("%H:%M")
    h = dt.hour % 12 or 12
    mer = "AM" if dt.hour < 12 else "PM"
    if dt.hour == 12 and dt.minute == 0:
        return "noon"
    if dt.hour == 0 and dt.minute == 0:
        return "midnight"
    return f"{h}:{dt.minute:02d} {mer}" if dt.minute else f"{h} {mer}"


def say_when(dt: datetime, now: datetime, h24: bool = False) -> str:
    """'7 AM', '7 AM tomorrow', '3:30 PM on Friday', '9 AM on December 3'."""
    clock = say_clock(dt, h24)
    days = (dt.date() - now.date()).days
    if days == 0:
        if dt.hour >= 18 and now.hour < 18:
            return f"{clock} tonight"
        return clock
    if days == 1:
        return f"{clock} tomorrow"
    if 1 < days < 7:
        return f"{clock} on {WEEKDAYS[dt.weekday()].capitalize()}"
    return f"{clock} on {_MONTHS[dt.month - 1].capitalize()} {dt.day}"
