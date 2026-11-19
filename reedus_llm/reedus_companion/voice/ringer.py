"""rings what assist/ says is due: a tone, then the words, over and over until you answer.

"Hey Reedus" quiets it at once (the wake word is the snooze bar's cousin); then "stop" or
"snooze" decides, and anything else counts as stop. it shares the speaker with the voice loop,
so it only plays between turns, holding the turn lock for one short cycle at a time.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Optional

from reedus_companion.voice import events

DIM, RESET = "\033[2m", "\033[0m"
SPEAK_EVERY = 3            # tone, tone, tone+words…


def announcement(it: dict, n_same: int = 1) -> str:
    from reedus_companion.assist import when
    if it["kind"] == "timer":
        if it.get("label"):
            return f"Your {it['label']} timer is done."
        return "Time's up." if n_same <= 1 else "Your timers are done."
    if it["kind"] == "alarm":
        at = when.say_clock(datetime.now())
        lead = "Good morning! " if datetime.now().hour < 11 else ""
        return f"{lead}It's {at}." + (f" {it['label']}." if it.get("label") else "")
    if it.get("missed"):
        return f"Earlier you asked me to remind you to {it['label']}."
    return f"Reminder: {it['label']}."


class Ringer:
    def __init__(self, runtime: dict, store=None):
        from reedus_companion.assist.store import assistant
        self.rt = runtime
        self.a = store or assistant()
        self._kick = threading.Event()
        self._playing = False
        self._stop = threading.Event()
        self.a.on_change(self._on_change)
        events.subscribe(self._on_event)

    def start(self) -> "Ringer":
        threading.Thread(target=self._run, name="ringer", daemon=True).start()
        if self.a.missed:
            names = ", ".join(i.get("label") or i["kind"] for i in self.a.missed)
            print(f"{DIM}  [assist] passed while I was off, dropped: {names}{RESET}", flush=True)
            self.a.missed.clear()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._kick.set()

    # --- listeners (other threads: never block) ---
    def _on_change(self, kind: str, it: dict) -> None:
        if kind == "ringing":
            print(f"{DIM}  [assist] ringing: {it['kind']} {it.get('label') or ''}{RESET}", flush=True)
            self._kick.set()

    def _on_event(self, kind: str, **_d) -> None:
        if kind == "wake" and self.a.ringing():
            self.a.hush()
            if self._playing:
                sp = self.rt.get("speaker")
                try:
                    if sp is not None:
                        sp.stop()                    # also flushes Lite's speaker (speech_abort)
                    else:
                        events.emit("speech_abort")
                except Exception:                                # noqa: BLE001
                    pass

    # --- the loop ---
    def _run(self) -> None:
        cycle = 0
        reminder_rounds = {}
        while not self._stop.is_set():
            ringing = [i for i in self.a.ringing() if i.get("state") == "ringing"]
            if not ringing:
                cycle = 0
                self._kick.wait(timeout=5.0)
                self._kick.clear()
                continue
            lock = self.rt.get("turn_lock")
            if lock is not None and not lock.acquire(timeout=0.5):
                continue                             # a turn is running: ring right after it
            pause = 0.6
            try:
                pause = self._cycle(ringing, cycle, reminder_rounds)
            except Exception as e:                               # noqa: BLE001
                print(f"{DIM}  [ringer] {type(e).__name__}: {e}{RESET}", flush=True)
                time.sleep(1.0)
            finally:
                self._playing = False
                if lock is not None:
                    lock.release()
            cycle += 1
            self._kick.wait(timeout=pause)            # outside the lock: a turn can start here
            self._kick.clear()

    def _cycle(self, ringing: list, cycle: int, reminder_rounds: dict) -> float:
        """One tone (+ words). Returns how long to wait before the next."""
        top = sorted(ringing, key=lambda i: ("alarm", "timer", "reminder").index(i["kind"]))[0]
        speaker = self.rt.get("speaker")
        tone = {"timer": "timer", "alarm": "alarm"}.get(top["kind"], "reminder")
        if top["kind"] == "reminder":
            rounds = reminder_rounds.get(top["id"], 0)
            if rounds >= 2:                           # a reminder says itself twice, then lets go
                self.a.dismiss([top["id"]])
                reminder_rounds.pop(top["id"], None)
                return 0.1
            reminder_rounds[top["id"]] = rounds + 1
            speak = True
        else:
            speak = cycle % SPEAK_EVERY == 0
        self._playing = True
        if speaker is not None and os.environ.get("REEDUS_RING_AUDIO", "1") != "0":
            from reedus_companion.voice.jingle import make_jingle
            if hasattr(speaker, "play_pcm"):
                speaker.play_pcm(make_jingle(tone), src_rate=24000)
            if speak and self._still(top):
                same = sum(1 for i in ringing if i["kind"] == top["kind"])
                line = announcement(top, same)
                print(f"reedus> {line}  {DIM}({top['kind']}){RESET}", flush=True)
                speaker.reset()
                speaker.say(line)
                speaker.finish(timeout=20.0)
        else:
            time.sleep(2.0)
        return 6.0 if top["kind"] == "reminder" else 0.6     # a beat before a reminder's second telling

    def _still(self, it: dict) -> bool:
        return any(i["id"] == it["id"] and i.get("state") == "ringing" for i in self.a.ringing())


_one: Optional[Ringer] = None


def start(runtime: dict) -> Optional[Ringer]:
    """Start once per process. REEDUS_ASSIST=0 turns the whole assistant layer off."""
    global _one
    if os.environ.get("REEDUS_ASSIST", "1") == "0":
        return None
    if _one is None:
        _one = Ringer(runtime).start()
    return _one
