# Reedus as an everyday assistant

This covers timers, alarms, reminders, lists, the day briefing and the glanceable face. It's one brain layer (`reedus_llm/reedus_companion/assist/`) that both bodies share. Lite also gets a display: the face shows countdowns, rings, and rotates info cards while nobody's talking.

## What you can say

Everything below is answered without a model round trip (`assist/commands.py`), the same way music commands skip the model. Anything it doesn't recognise falls through to the persona as before.

| | Examples |
|---|---|
| Timers | "set a timer for 10 minutes" · "12 minute pasta timer" · "timer for 5 minutes for the eggs" · "how much time is left?" · "add 2 minutes to the pasta timer" · "pause the timer" · "cancel the eggs timer" · "cancel all timers" |
| Alarms | "wake me up at 6:30" · "set an alarm for 7 every weekday" · "alarm for quarter to 8 called gym" · "wake me up in 20 minutes" · "when's my alarm?" · "cancel my 7 AM alarm" |
| Reminders | "remind me to call Mom at 5" · "remind me in 20 minutes to flip the laundry" · "remind me tomorrow at 8 to bring my laptop" · "remind me to take my meds every day at 9" · "remind me to email Professor Lee" (it asks when) |
| While ringing | "stop" · "okay" · "snooze" · "snooze for 5 minutes". Saying "Reedus" quiets it at once. Anything else you say stops it and gets handled normally |
| Lists | "add milk, eggs and mac and cheese to my shopping list" · "put call the dentist on my to-do list" · "what's on my shopping list?" · "check off eggs" · "clear my shopping list" |
| Your day | "what's my day look like?" · "what's on my calendar tomorrow?" · "what's my next class?" · "give me my briefing" · "what do I have this week?" |

Some decisions worth knowing:
- **A bare hour for an alarm means AM between 4 and 11** ("alarm for 7" means 7 AM). Otherwise it picks the soonest one. The reply always says AM or PM.
- **A reminder on a day with no time is set for 9 AM** ("remind me Friday to study").
- **When several timers match, it asks which one.** "the pasta one", "the second one" or "both" answers it.
- **Snooze is 9 minutes** (`REEDUS_SNOOZE_S`). Alarms give up after 10 minutes of ringing, timers after 5. A reminder says itself twice.
- **If the brain was off when something came due:** missed timers and alarms are dropped and logged. Repeating alarms roll forward. A reminder up to 12 hours late is still delivered ("Earlier you asked me to remind you to…").

## The face

| When | Lite shows |
|---|---|
| A timer under 10 min | `4:32` in big digits with two little eyes peeking over it, a shrinking bar along the bottom, and the label scrolling by every 12 s. The last 10 seconds go coral |
| A timer of 10 to 100 min | minutes stacked over seconds |
| A longer timer | hours over minutes |
| Several timers | the soonest-ending one (as the Echo Dot with Clock does), plus a gold dot per extra timer |
| Ringing | flashing `0:00` for a timer. A swinging bell and the time for an alarm. A bell and scrolling words for a reminder |
| Alarm set for the next 24 h | one dim gold pixel in the corner, like a bedside clock |
| Idle, every 2 min (configurable) | time, then weather, then next class or event, then what's playing, then back to the face. 10 PM to 7 AM: just the time, dimmed |

A conversation always takes the panel back. The run box decides what to show (`lite/glance.py`); the device draws and ticks it (`firmware/src/glance.cpp`), so a Wi-Fi hiccup doesn't freeze a countdown. Serial `glance timer|ring|alarm|clock|weather|event|music|off` shows each screen on the bench without a run box. The pixel fonts come from `tools/gen_font.py`, which writes both `font.h` and ReedOS's `font.js`.

## Calendars

ReedOS → Settings → Connections → Calendars takes a private **iCal link**: read-only and no sign-in.
- **Google:** calendar settings, then "Secret address in iCal format".
- **iCloud:** share the calendar as public, then copy the webcal link.
- **Outlook:** Settings, then Shared calendars, then Publish, then ICS.
- **Canvas:** Calendar, then Calendar Feed. This puts classes and assignment due dates in one feed.

The reader (`assist/calendar.py`) handles weekly class schedules, moved and cancelled instances, skipped dates, monthly "first Thursday" events, and Outlook's Windows time zone names. Links stay on the run box in `memory/assist.json` (gitignored), and ReedOS only ever sees the host. With no iCal link but a Composio hub connected, "what's my day" asks Google Calendar through the hub instead. That path is slower and doesn't feed the face card.

## ReedOS

The Lite tab has four new cards:
- **Timers & alarms:** a live list with pause and cancel, quick 1/5/10/25 min buttons, a "say it" box that runs the same commands as voice, and Stop/Snooze while something rings.
- **Today:** today's events from your calendars.
- **Lists:** shopping and to-do, with check-off.
- **Glance screen:** turn rotation on or off, how often, which cards, 24 h clock, and a Show button per card to preview it on the real panel.

The face preview draws the countdown and cards exactly as the panel does (`js/glance.js` is a port of `glance.cpp`). Demo mode runs all of it with no run box.

## Settings and env

| | |
|---|---|
| `REEDUS_ASSIST=0` | turns the whole layer off |
| `REEDUS_RING_AUDIO=0` | rings silently (face only) |
| `REEDUS_SNOOZE_S` | snooze length, default 540 |
| `REEDUS_USER_NAME` | "Good morning, Danial." in the briefing |
| `REEDUS_UNITS=C` | Celsius on the weather card |
| `REEDUS_CITY` / `REEDUS_LAT`,`REEDUS_LON` | where the weather is (already used by the weather fast path) |

## Tested

- `reedus_llm/tests/test_assist.py`: **all pass**, 120+ checks, pinned clocks, no network. It covers:
  - spoken times
  - every command above
  - ring, hush, snooze, expiry and repeating rollover
  - iCal recurrence: across DST, with an EXDATE, a moved instance, UNTIL, a monthly BYDAY with COUNT, and DURATION
  - the briefing
  - the glance director against a fake hub
  - the API, including that calendar links never leave the brain
  - persistence across a restart
- The rest of `reedus_llm/tests/` gives the same results as before the change. `test_knowledge`, `test_router_split` and `test_wakeword` already failed on Windows (file encoding, model path), and they fail the same way now.
- `glance.cpp` compiles warning-free with host g++ against Arduino/FastLED/JSON stubs, and every screen was rendered and checked as text art.
- **Not done:** a PlatformIO build for the ESP32 (no `pio` on this laptop, so run `pio run`). Nothing has been tried on real hardware. Ringing hasn't been tried through a live voice loop on the Xeon.

## What's next

What people actually use Alexa for is narrow. Weather, music and timers have topped the surveys for years, with smart home, schedule checks and quick questions after them. Alexa+ adds agentic actions (booking, family calendars, meal plans, memory across devices). Reedus already had weather, Spotify control, web answers, MCP app hubs (Composio, Zapier, Home Assistant) and a personality. This change covers timers, alarms, reminders, lists and calendar. What's left, ranked by how often people would use it against how hard it is:

**Do next (high use, software only):**
1. ~~**Music out of Lite's own speaker.**~~ Done: see `MUSIC.md`.
2. **Routines.** "Good morning" could run the briefing, start a playlist and turn the lights up. Triggers could be a phrase, a time, sunrise, or an alarm being dismissed. ReedOS already has a routine engine for the robot, and it should grow voice and time triggers that both bodies share.
3. **Smart home made obvious.** Home Assistant already connects as an MCP hub. Add fast paths for "turn off the lights" and "set it to 70" so they skip the model, plus a Lite card when something changes.
4. **Proactive nudges.** For example: "your 2:30 class is in 15 minutes", an evening "assignment due tomorrow" (Canvas puts these in the calendar feed), "take an umbrella" when rain is coming, "package arriving today". These are cheap: the calendar and weather plumbing is already there.
5. **Announcements and an intercom.** "Tell the robot to say dinner's ready." Two bodies share one brain today; the ReedOS phone app could become a third speaker.

**Then (medium):**
- Sleep and ambient sounds (rain, brown noise) with a sleep timer, and a sunrise alarm where the face slowly brightens before the tone.
- Podcasts, radio and audiobooks (Spotify covers podcasts once item 1 is done).
- News and sports briefings, and "Reedus, what's the score?" Search already does scores, so this is just a fast path plus a card.
- Voice profiles (whose list, whose calendar) and a whisper mode at night (quieter TTS after 10 PM).
- Find my phone (ring it through the ReedOS app), unit conversions, recipes step by step with timers attached ("next step").
- A shopping-list sync to Todoist, Google Tasks or AnyList through Composio, so the list on your phone matches.
- Sound detection on the XVF3800 (smoke alarm, glass breaking, a baby crying) through a small on-device or run-box classifier. This is Alexa Guard, and Lite already has the mics.

**Beyond Alexa (what makes it Reedus):**
- Memory that matters. It knows your classes, so it can ask how the CHEM exam went, and it remembers you mentioned a dentist appointment.
- Study mode: Pomodoro on the face (a 25 min focus timer is already one tap), quiz-me from your notes, "explain this" using the corpus.
- Conversations that start on Lite and finish on the robot ("follow me"), since it's one brain.
- The face as a real status display: Discord or Slack pings, battery, a build passing, all through the same `glance` card protocol with no firmware change for new text cards.

## Where the code is

| Path | What |
|---|---|
| `reedus_llm/reedus_companion/assist/when.py` | spoken durations, clock times, days, repeats |
| `assist/store.py` | timers, alarms, reminders, lists, settings; the clock that fires them; `memory/assist.json` |
| `assist/commands.py` | the voice commands (fast path in `serve/core.py`, before the Spotify fast path) |
| `assist/calendar.py`, `assist/briefing.py` | iCal feeds and "what's my day" |
| `assist/api.py` | `GET/POST /api/assist` for ReedOS |
| `voice/ringer.py` | rings through the voice loop's speaker between turns; the wake word hushes it |
| `lite/glance.py` | what the face shows and when |
| `ReedusLite/firmware/src/glance.cpp`, `font.h` | the drawing on the panel |
| `ReedOS/reedos-app/src/js/assist.js`, `glance.js`, `font.js` | the app cards and the preview |
