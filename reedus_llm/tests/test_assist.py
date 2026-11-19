#!/usr/bin/env python3
"""the assistant layer: spoken times, timer/alarm/reminder/list commands, ringing (hush, snooze,
expiry, repeats), iCal feeds with recurring classes, the day briefing, the Lite glance director,
and the ReedOS API. stdlib only, no network, pinned clocks."""
from __future__ import annotations
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = Path(tempfile.mkdtemp())
os.environ["REEDUS_ASSIST_FILE"] = str(TMP / "assist.json")
os.environ["REEDUS_WEATHER"] = "0"

from reedus_companion.assist import when                              # noqa: E402
from reedus_companion.assist import calendar as cal                   # noqa: E402
from reedus_companion.assist.commands import Commands, clean          # noqa: E402
from reedus_companion.assist.store import Assistant                   # noqa: E402

_fails = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


class Clock:
    def __init__(self, dt: datetime):
        self.t = dt.timestamp()

    def __call__(self):
        return self.t

    def dt(self):
        return datetime.fromtimestamp(self.t)


NOW = datetime(2026, 11, 15, 14, 5)          # a Sunday, 2:05 PM


def fresh(now=NOW):
    clk = Clock(now)
    a = Assistant(TMP / f"a{id(clk)}.json", clock=clk)
    return a, Commands(a, now=clk.dt), clk


print("[1] spoken time")
n = when.norm
check(when.parse_duration(n("twenty five minutes")) == 1500, "twenty five minutes")
check(when.parse_duration(n("an hour and a half")) == 5400, "an hour and a half")
check(when.parse_duration(n("two and a half hours")) == 9000, "two and a half hours")
check(when.parse_duration(n("90 seconds")) == 90, "90 seconds")
check(when.parse_duration(n("half an hour")) == 1800, "half an hour")
check(when.parse_duration(n("1 hour 30 minutes")) == 5400, "1 hour 30 minutes")
check(when.parse_clock(n("5 minutes")) is None, "a duration isn't a clock time")
check(when.parse_clock(n("at 50 percent")) is None, "a percentage isn't a clock time")
c = when.parse_clock(n("quarter to 8 in the morning"))
check(c and c[:3] == (7, 45, "am"), "quarter to 8 in the morning -> 7:45 am")
c = when.parse_clock(n("half past six tonight"))
check(c and c[:3] == (6, 30, "pm"), "half past six tonight -> 6:30 pm")
check(when.resolve_clock(7, 0, None, NOW) == datetime(2026, 11, 15, 19, 0), "ambiguous 7 at 2pm -> 7pm today")
check(when.parse_day(n("next sunday"), NOW)[0].day == 22, "next sunday said on a sunday -> a week out")
check(when.parse_repeat(n("every monday and wednesday"))[0] == [0, 2], "every monday and wednesday")
check(when.parse_repeat(n("weekdays"))[0] == [0, 1, 2, 3, 4], "weekdays")
check(when.say_duration(3725) == "1 hour, 2 minutes and 5 seconds", "say_duration")

print("[2] timers")
a, cmd, clk = fresh()
r = cmd.handle("Reedus, set a pasta timer for twelve minutes")
check(r and "Pasta timer" in r and "12 minutes" in r, f"labelled timer: {r!r}")
r = cmd.handle("10 minute pizza timer")
check(r and r.startswith("Pizza timer"), f"label before 'timer': {r!r}")
r = cmd.handle("set a timer for 5 minutes for the eggs")
check(r and r.startswith("Eggs timer"), f"label after 'for the': {r!r}")
check(len(a.active("timer")) == 3, "three timers running")
r = cmd.handle("how much time is left on the pasta timer")
check(r and "3 timers" in r, f"several timers listed: {r!r}")
r = cmd.handle("add 2 minutes to the pasta timer")
check(r and "14 minutes" in r, f"extend by label: {r!r}")
r = cmd.handle("pause the pizza timer")
check(r and r.startswith("Paused") and a.find("timer", "pizza")[0]["state"] == "paused", "pause by label")
clk.t += 60
check(abs(a.left(a.find("timer", "pizza")[0]) - 600) < 1, "a paused timer doesn't run down")
cmd.handle("resume the pizza timer")
check(a.find("timer", "pizza")[0]["state"] == "active", "resume")
r = cmd.handle("cancel the timer")
check(r and r.endswith("Which one?") and a.pending, "ambiguous cancel asks which")
r = cmd.handle("the eggs one")
check(r == "Eggs timer cancelled.", f"answer picks it: {r!r}")
r = cmd.handle("cancel all timers")
check(r == "Cancelled all 2 timers." and not a.active("timer"), "cancel all")
check(cmd.handle("set a timer") == "How long should the timer be?", "timer with no length asks")
check(cmd.handle("how long is the movie") is None, "a non-timer 'how long' falls through")

print("[3] ringing")
a, cmd, clk = fresh()
heard = []
a.on_change(lambda k, it: heard.append(k))
cmd.handle("set a timer for 30 seconds")
clk.t += 31
a.tick()
check(a.ringing() and "ringing" in heard, "a due timer rings")
check(cmd.handle("stop") in ("Timer stopped.", "Okay, timer's off.", "Done."), "stop silences it")
check(not a.ringing() and not a.active(), "a stopped one-shot timer is gone")
check(cmd.handle("stop") is None, "'stop' with nothing ringing falls through (to music etc)")

cmd.handle("wake me up at 7 tomorrow")
al = a.active("alarm")[0]
check(datetime.fromtimestamp(al["due"]) == datetime(2026, 11, 16, 7, 0), "alarm for 7 tomorrow is 7 AM")
clk.t = al["due"] + 1
a.tick()
a.hush()
check(a.ringing()[0]["state"] == "hushed", "the wake word hushes it")
r = cmd.handle("snooze")
check(r and r.startswith("Snoozing for 9 minutes") and a.active("alarm"), "snooze puts it back 9 minutes")
clk.t += 9 * 60 + 1
a.tick()
check(a.ringing(), "and it rings again")
clk.t += 700
a.tick()
check(not a.ringing(), "it gives up after ringing 10 minutes")

a, cmd, clk = fresh()
cmd.handle("set an alarm for 7 every weekday")
al = a.active("alarm")[0]
check(datetime.fromtimestamp(al["due"]) == datetime(2026, 11, 16, 7, 0), "weekday alarm starts Monday 7 AM")
clk.t = al["due"] + 1
a.tick()
cmd.handle("okay i'm up")
nxt = datetime.fromtimestamp(a.active("alarm")[0]["due"])
check(nxt == datetime(2026, 11, 17, 7, 0), "a repeating alarm rolls to the next weekday")
clk.t = datetime(2026, 11, 17, 7, 0, 1).timestamp()
a.tick()
a.hush()
clk.t += 13
a.tick()
check(not a.ringing() and a.active("alarm"), "hushed and never answered counts as stopped, repeat kept")
check(cmd.handle("what's the weather") is None, "other talk falls through")

a, cmd, clk = fresh()
cmd.handle("set a timer for 1 minute")
clk.t += 61
a.tick()
check(cmd.handle("what time is it") is None and not a.ringing(),
      "saying something else while ringing stops it and lets the turn through")

print("[4] alarms and reminders")
a, cmd, clk = fresh()
r = cmd.handle("set an alarm for 7")
check(r and r.startswith("Alarm set for 7 AM tomorrow"), f"bare 7 for an alarm means AM: {r!r}")
r = cmd.handle("set an alarm for 9 pm")
check(r and "9 PM tonight" in r, f"explicit pm: {r!r}")
r = cmd.handle("set an alarm")
check(r == "What time should I set it for?", "alarm with no time asks")
r = cmd.handle("eight thirty")
check(r and "8:30 AM" in r, f"the answer sets it: {r!r}")
r = cmd.handle("when is my alarm")
check(r and "3 alarms" in r, f"alarm query: {r!r}")
r = cmd.handle("cancel my 9 pm alarm")
check(r and "9 PM" in r and len(a.active("alarm")) == 2, f"cancel by time: {r!r}")
r = cmd.handle("remind me to call my Mom at 5")
check(r and "call your Mom at 5 PM" in r, f"reminder at 5 (pronouns + capitals kept): {r!r}")
r = cmd.handle("remind me in 20 minutes to take out the laundry")
check(r and "in 20 minutes" in r, f"relative reminder: {r!r}")
r = cmd.handle("remind me tomorrow at 8 to bring my laptop")
check(r and "8 AM tomorrow" in r, f"tomorrow at 8 -> 8 AM: {r!r}")
r = cmd.handle("remind me to take my meds every day at 9am")
check(r and "9 AM every day" in r, f"daily reminder: {r!r}")
r = cmd.handle("remind me on friday to study for chem")
check(r and "9 AM on Friday" in r, f"a day with no time -> 9 AM: {r!r}")
r = cmd.handle("remind me to email Professor Lee")
check(r == "When should I remind you to email Professor Lee?", f"no time asks: {r!r}")
r = cmd.handle("in an hour")
check(r and "email Professor Lee in 1 hour" in r, f"the answer sets it: {r!r}")
r = cmd.handle("what are my reminders")
check(r and "6 reminders" in r, f"reminder query: {r!r}")
r = cmd.handle("cancel my reminder to call mom")
check(r and "call your Mom" in r, f"cancel by words: {r!r}")
rem = a.find("reminder", "laundry")[0]
clk.t = rem["due"] + 1
a.tick()
check(a.ringing()[0]["label"] == "take out the laundry", "a reminder rings with its words")

print("[5] lists")
a, cmd, clk = fresh()
r = cmd.handle("add milk, eggs and mac and cheese to my shopping list")
check(r == "Added milk, eggs and mac and cheese to your shopping list.", f"split items, keep pairs: {r!r}")
r = cmd.handle("put bread on the grocery list")
check(a.list_items("shopping")[1][-1] == "bread", "grocery is the shopping list")
r = cmd.handle("add Milk to my shopping list")
check(r and "already" in r, "no duplicates")
cmd.handle("add call the dentist to my to do list")
check(a.list_items("to-do")[1] == ["call the dentist"], "to do -> to-do")
r = cmd.handle("what's on my shopping list")
check(r == "On your shopping list: milk, eggs, mac and cheese and bread.", f"read back: {r!r}")
r = cmd.handle("remove eggs from my shopping list")
check(r and "eggs" in r and "eggs" not in a.list_items("shopping")[1], "remove")
r = cmd.handle("check off call the dentist")
check(r == "Checked off call the dentist.", "check off searches every list")
r = cmd.handle("clear my shopping list")
check(r == "Cleared your shopping list." and not a.list_items("shopping")[1], "clear")
check(cmd.handle("add 5 minutes to the timer") == "You don't have a timer running.", "'add X to the timer' isn't a list")

print("[6] wake classifier lets assistant commands through")
from reedus_companion.voice.wake import classify_utterance  # noqa: E402
check(classify_utterance("Reedus cancel the timer")[0] == "wake_cmd", "'Reedus cancel the timer' is a command")
check(classify_utterance("cancel my alarm")[0] == "turn", "'cancel my alarm' in session is a turn")
check(classify_utterance("cancel that")[0] == "dismiss", "'cancel that' is still a never-mind")
check(classify_utterance("Reedus stop")[1] == "stop", "'Reedus stop' reaches the brain as 'stop'")

print("[7] iCal feeds")
ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:America/New_York
END:VTIMEZONE
BEGIN:VEVENT
UID:chem@x
SUMMARY:CHEM 101 Lecture
LOCATION:Hall B\\, Room 2
DTSTART;TZID=America/New_York:20260831T093000
DTEND;TZID=America/New_York:20260831T104500
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261211T235959Z
EXDATE;TZID=America/New_York:20261125T093000
END:VEVENT
BEGIN:VEVENT
UID:chem@x
RECURRENCE-ID;TZID=America/New_York:20261118T093000
SUMMARY:CHEM 101 Lecture (moved)
DTSTART;TZID=America/New_York:20261118T130000
DTEND;TZID=America/New_York:20261118T141500
END:VEVENT
BEGIN:VEVENT
UID:dentist@x
SUMMARY:Dentist
DTSTART:20261116T200000Z
DTEND:20261116T203000Z
END:VEVENT
BEGIN:VEVENT
UID:bday@x
SUMMARY:Mom's birthday
DTSTART;VALUE=DATE:20261116
DTEND;VALUE=DATE:20261117
END:VEVENT
BEGIN:VEVENT
UID:club@x
SUMMARY:Robotics club
DTSTART;TZID=America/New_York:20260903T180000
DURATION:PT1H30M
RRULE:FREQ=MONTHLY;BYDAY=1TH;COUNT=6
END:VEVENT
BEGIN:VEVENT
UID:gone@x
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261116T150000Z
DTEND:20261116T160000Z
END:VEVENT
END:VCALENDAR
"""
from zoneinfo import ZoneInfo  # noqa: E402
NY = ZoneInfo("America/New_York")
evs = cal.parse_ics(ICS)
check(len(evs) == 6, f"six VEVENTs parsed ({len(evs)})")
check(evs[0]["location"] == "Hall B, Room 2", "escaped comma in LOCATION")
mon = datetime(2026, 11, 16, tzinfo=NY)
occ = cal.occurrences(evs, mon, mon + timedelta(days=1))
titles = [o["title"] for o in occ]
check(titles[0] == "Mom's birthday" and occ[0]["all_day"], "all-day event first")
check("CHEM 101 Lecture" in titles and "Dentist" in titles, f"monday has class + dentist: {titles}")
check("Cancelled thing" not in titles, "cancelled events dropped")
chem = next(o for o in occ if o["title"] == "CHEM 101 Lecture")
check(chem["start"].astimezone(NY).hour == 9 and chem["start"].astimezone(NY).minute == 30,
      "class stays 9:30 local after the DST change (Nov)")
wed = cal.occurrences(evs, datetime(2026, 11, 18, tzinfo=NY), datetime(2026, 11, 19, tzinfo=NY))
check([o["title"] for o in wed] == ["CHEM 101 Lecture (moved)"] and wed[0]["start"].astimezone(NY).hour == 13,
      "a moved instance replaces the original slot")
thanks = cal.occurrences(evs, datetime(2026, 11, 25, tzinfo=NY), datetime(2026, 11, 26, tzinfo=NY))
check(not thanks, "EXDATE removes Thanksgiving week's class")
dec = cal.occurrences(evs, datetime(2026, 12, 14, tzinfo=NY), datetime(2026, 12, 19, tzinfo=NY))
check(not [o for o in dec if "CHEM" in o["title"]], "UNTIL ends the semester")
club = cal.occurrences(evs, datetime(2026, 9, 1, tzinfo=NY), datetime(2027, 6, 1, tzinfo=NY))
club = [o for o in club if o["title"] == "Robotics club"]
check(len(club) == 6 and club[1]["start"].date().isoformat() == "2026-10-01", "monthly first-Thursday, COUNT=6")
check(club[0]["end"] - club[0]["start"] == timedelta(minutes=90), "DURATION sets the end")
check(cal._zone("Eastern Standard Time") is not None, "Outlook's Windows zone names resolve")

print("[8] the day, from a calendar")
a, cmd, clk = fresh(datetime(2026, 11, 16, 8, 0))
a.settings["calendars"] = [{"id": "c1", "name": "Classes", "url": "https://example.test/c.ics", "enabled": True}]
from reedus_companion.assist import briefing  # noqa: E402
import reedus_companion.assist.store as store_mod  # noqa: E402
store_mod._one = a                                            # briefing reads the singleton
briefing._cals = cal.Calendars(lambda: a.settings["calendars"], fetcher=lambda url: ICS)
local = datetime(2026, 11, 16, 8, 0).astimezone()
nx = briefing.next_event(local, within_h=24)
check(nx and nx["title"] in ("CHEM 101 Lecture", "Dentist"), f"next event: {nx and nx['title']}")
r = briefing.day_summary(datetime(2026, 11, 16), datetime(2026, 11, 16, 8, 0), weather=False)
check("CHEM 101 Lecture" in r and "Dentist" in r and "Mom's birthday, all day" in r, f"briefing: {r!r}")
cmd.handle("add finish lab report to my to do list")
r = briefing.day_summary(datetime(2026, 11, 16), datetime(2026, 11, 16, 8, 0), weather=False)
check("finish lab report" in r, "to-dos make the briefing")
r = cmd.handle("what's my next class")
check(r and ("CHEM 101" in r or "in" in r), f"next class: {r!r}")
r = cmd.handle("what's on my calendar tomorrow")
check(r and "Tuesday" not in r, f"tomorrow: {r!r}")
store_mod._one = None

print("[9] glance director -> Lite")


class FakeHub:
    def __init__(self):
        self.sent, self.connected, self.last_state, self.display = [], True, "idle", {}
        self._l = []

    def send(self, m):
        self.sent.append(m)

    def on_message(self, fn):
        self._l.append(fn)


from reedus_companion.lite import glance as gl  # noqa: E402
a, cmd, clk = fresh()
hub = FakeHub()
d = gl.GlanceDirector(hub, store=a, clock=clk)
d.step()
kinds = [m["type"] for m in hub.sent]
check(kinds[:2] == ["clock", "timers"], f"a fresh link gets the time and the timers: {kinds}")
cmd.handle("set a pasta timer for 4 minutes and 32 seconds")
hub.sent.clear()
d.step()
tm = [m for m in hub.sent if m["type"] == "timers"]
check(tm and tm[0]["items"][0]["label"] == "PASTA" and abs(tm[0]["items"][0]["left"] - 272) < 1,
      "a new timer goes out with its seconds, label in panel letters")
hub.sent.clear()
d.step()
check(not [m for m in hub.sent if m["type"] == "timers"], "unchanged timers aren't resent every second")
clk.t += 300
d._next_round = 0
d._last_busy = clk.t - 100
hub.sent.clear()
d.step()
check(not [m for m in hub.sent if m["type"] == "glance"], "no idle cards while a timer owns the face")
a.cancel([i["id"] for i in a.active()])
clk.t += 1
gl.CARD_HOLD.update({k: 0.0 for k in gl.CARD_HOLD})          # don't actually wait between cards
d._wait_idle = lambda s: True
d._next_round = 0
hub.sent.clear()
d.step()
cards = [m["card"] for m in hub.sent if m["type"] == "glance"]
check(cards == ["clock"], f"idle round: clock (no weather/calendar/music here): {cards}")
hub.last_state = "listening"
d._next_round = 0
hub.sent.clear()
d.step()
check(not [m for m in hub.sent if m["type"] == "glance"], "no cards mid-conversation")
check(gl.panel_text("Café Ñoño — Lab #3!") == "CAFE NONO LAB #3!", "panel text folds accents, drops what the font can't draw")
cmd.handle("set a timer for 10 seconds")
clk.t += 11
a.tick()
d.step()
ring = [m for m in hub.sent if m["type"] == "timers"][-1]["ring"]
check(ring and ring[0]["kind"] == "timer", "ringing goes to the face")

print("[10] ReedOS API")
import reedus_companion.assist.api as api  # noqa: E402
a, cmd, clk = fresh()
store_mod._one = a
out = api.act({"action": "timer", "seconds": 300, "label": "tea"})
check(out.get("ok") and out["item"]["label"] == "tea", "add a timer")
check(api.act({"action": "timer", "seconds": 0}).get("error"), "a zero timer is refused")
out = api.act({"action": "alarm", "at": "07:15", "days": [0, 1, 2, 3, 4]})
check(out.get("ok") and out["item"]["repeat"] == [0, 1, 2, 3, 4], "weekday alarm from the app")
out = api.act({"action": "say", "text": "add oat milk to my shopping list"})
check(out["reply"] == "Added oat milk to your shopping list.", "say routes through the commands")
a.settings["calendars"] = [{"id": "c9", "name": "Classes", "url": "https://calendar.google.com/calendar/ical/secret/basic.ics"}]
briefing._cals = cal.Calendars(lambda: a.settings["calendars"], fetcher=lambda url: ICS)
st = api.status()
check(st["settings"]["calendars"][0]["host"] == "calendar.google.com" and "url" not in st["settings"]["calendars"][0],
      "status shows the calendar host, never the secret link")
out = api.act({"action": "settings", "glance": {"every": 5, "cards": ["clock", "bogus"]}})
check(a.settings["glance"]["every"] == 30 and a.settings["glance"]["cards"] == ["clock"], "settings are clamped")
check(api.act({"action": "calendar.add", "url": "ftp://nope"}).get("error"), "a non-http calendar link is refused")
store_mod._one = None

print("[11] persistence")
p = TMP / "persist.json"
clk = Clock(NOW)
a1 = Assistant(p, clock=clk)
a1.add_timer(60, "tea")
a1.add_alarm(NOW.timestamp() + 3600, "", [0, 1, 2, 3, 4, 5, 6])
a1.add_reminder(NOW.timestamp() + 120, "stretch")
a1.list_add("shopping", ["milk"])
clk.t += 4 * 3600                                       # the brain was off for 4 hours
a2 = Assistant(p, clock=clk)
check([m["label"] for m in a2.missed] == ["tea"], "a timer that ended while off is dropped (and reported)")
check(a2.active("alarm") and a2.active("alarm")[0]["due"] > clk.t, "a repeating alarm rolls forward")
rem = [i for i in a2.items if i["kind"] == "reminder"]
check(rem and rem[0].get("missed"), "a missed reminder is kept and delivered late")
check(a2.list_items("shopping")[1] == ["milk"], "lists survive")

print()
if _fails:
    print(f"{len(_fails)} FAILED")
    for f in _fails:
        print("  -", f)
    sys.exit(1)
print("all passed")
