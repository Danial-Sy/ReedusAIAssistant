"""streaming speech: cut sentences as tokens land, synth + play in two stages."""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import Optional

from reedus_companion import config
from reedus_companion.voice import speech_text

_SENT_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s|$)")

_MIN_CHARS = 28


class StreamSpeaker:
    """Feed it tokens; it speaks complete sentences (after gen by default)."""

    def __init__(self, tts, min_chars: int = _MIN_CHARS, debug: bool = False,
                 scrub=None, overlap: Optional[bool] = None):
        self.tts = tts
        self.min_chars = min_chars
        self.debug = debug
        self.scrub = scrub
        if overlap is None:
            overlap = os.environ.get("REEDUS_TTS_OVERLAP", "0") == "1"
        self.overlap = bool(overlap)
        self._buf = ""
        self._deferred: list[str] = []
        # two stages: sentence N+1 is synthesized during playback of N, otherwise there's a hole
        # at every boundary. one USB stream for the whole turn, restarting the DAC per sentence
        # ate the first ~100ms of every clip.
        self._text_q: "queue.Queue[Optional[str]]" = queue.Queue()
        self._audio_q: "queue.Queue[Optional[object]]" = queue.Queue(maxsize=4)
        self._synth_t: Optional[threading.Thread] = None
        self._play_t: Optional[threading.Thread] = None
        self._spoken: list[str] = []
        self._streamed_any = False
        self._error: Optional[BaseException] = None
        self._abort = threading.Event()
        self._pb = None                     # active PlaybackStream (for stop())
        # audio-on-the-wire accounting. playing() only says the player thread exists, which is
        # the whole turn, so think-barge was dead after any spoken ack. _pending counts unplayed
        # chunks, _quiet_at covers the DAC buffer still sounding after the last write.
        self._pending = 0
        self._quiet_at = 0.0
        self._pending_lock = threading.Lock()
        self._idle_close_s = float(os.environ.get("REEDUS_TTS_IDLE_CLOSE", "1.2"))

    # --- producer side ---
    def feed(self, piece: str) -> None:
        """on_token hook. Cheap and non-blocking, never synthesize on this thread."""
        if self._abort.is_set():
            return
        self._buf += piece
        while True:
            sentence = self._take_sentence()
            if sentence is None:
                return
            self._enqueue(sentence, from_stream=True)

    def _take_sentence(self) -> Optional[str]:
        m = _SENT_END.search(self._buf)
        if not m:
            return None
        if m.end() < self.min_chars:
            nxt = _SENT_END.search(self._buf, m.end())
            if nxt is None:
                return None
            m = nxt
        sentence, self._buf = self._buf[:m.end()], self._buf[m.end():]
        return sentence.strip()

    def say(self, line: str) -> None:
        """Speak a line right now, outside the token stream."""
        self._enqueue(line, from_stream=False)

    def _enqueue(self, sentence: str, from_stream: bool = False) -> None:
        if self.scrub is not None:
            sentence = self.scrub(sentence)
            if not sentence.strip():
                return
        spoken = speech_text.clean_for_speech(sentence)
        if not spoken.strip():
            return
        self._spoken.append(spoken)
        if from_stream:
            self._streamed_any = True
            if not self.overlap:
                self._deferred.append(spoken)
                return
        self._ensure_workers()
        self._mark_pending(+1)
        self._text_q.put(spoken)

    def _flush_deferred(self) -> None:
        if not self._deferred:
            return
        self._ensure_workers()
        for spoken in self._deferred:
            self._mark_pending(+1)
            self._text_q.put(spoken)
        self._deferred.clear()

    # --- busy accounting ---
    def _mark_pending(self, delta: int) -> None:
        with self._pending_lock:
            self._pending = max(0, self._pending + delta)
            if delta < 0:
                self._quiet_at = time.monotonic() + 0.30

    def speaking(self) -> bool:
        """True while TTS audio is (about to be) on the wire."""
        if self._abort.is_set():
            return False
        with self._pending_lock:
            if self._pending > 0:
                return True
            return time.monotonic() < self._quiet_at

    def _ensure_workers(self) -> None:
        if self._synth_t is None:
            self._synth_t = threading.Thread(target=self._synth_loop, daemon=True)
            self._synth_t.start()
        if self._play_t is None:
            self._play_t = threading.Thread(target=self._play_loop, daemon=True)
            self._play_t.start()

    # --- consumer side ---
    def _synth_loop(self) -> None:
        while True:
            item = self._text_q.get()
            if item is None:
                self._put_audio(None)
                return
            try:
                audio = self.tts.synth(item)
                if audio is not None and getattr(audio, "size", 0):
                    if not self._put_audio(audio):
                        self._mark_pending(-1)
                else:
                    self._mark_pending(-1)
            except BaseException as e:              # noqa: BLE001
                self._mark_pending(-1)
                self._error = e
                if self.debug:
                    print(f"[stream_speaker] synth: {type(e).__name__}: {e}")

    def _put_audio(self, audio) -> bool:
        """Blocking put that gives up when stop() aborted the turn."""
        while not self._abort.is_set():
            try:
                self._audio_q.put(audio, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _play_loop(self) -> None:
        from reedus_companion.voice.tts import PlaybackStream
        pb = PlaybackStream(self.tts.sample_rate, device=config.TTS_DEVICE)
        self._pb = pb
        idle_since = None
        try:
            while not self._abort.is_set():
                try:
                    audio = self._audio_q.get(timeout=0.04)
                except queue.Empty:
                    if self._abort.is_set():
                        break
                    if self._pending > 0:
                        pb.pad(0.04)                # synth is late, keep the DAC fed
                        idle_since = None
                        continue
                    if idle_since is None:
                        idle_since = time.monotonic()
                    elif (self._idle_close_s > 0
                          and time.monotonic() - idle_since >= self._idle_close_s):
                        pb.close()
                        idle_since = None
                    continue
                idle_since = None
                if audio is None:
                    pb.pad(0.06)
                    return
                try:
                    pb.write(audio)
                except BaseException as e:          # noqa: BLE001
                    self._error = e
                    if self.debug:
                        print(f"[stream_speaker] play: {type(e).__name__}: {e}")
                    if self._abort.is_set():
                        break
                finally:
                    self._mark_pending(-1)
        finally:
            pb.close()
            self._pb = None

    def stop(self) -> None:
        """Hard-stop synth + playback (barge-in). Safe to call from another thread."""
        self._abort.set()
        # unblock workers waiting on queues
        try:
            self._text_q.put_nowait(None)
        except Exception:
            pass
        try:
            self._audio_q.put_nowait(None)
        except Exception:
            pass
        pb = self._pb
        if pb is not None:
            try:
                pb.close()
            except Exception:
                pass
            self._pb = None
        self._deferred.clear()
        self._buf = ""
        with self._pending_lock:
            self._pending = 0
            self._quiet_at = 0.0
        t_s, t_p = self._synth_t, self._play_t
        if t_s is not None and t_s.is_alive() and threading.current_thread() is not t_s:
            t_s.join(timeout=1.0)
        if t_p is not None and t_p.is_alive() and threading.current_thread() is not t_p:
            t_p.join(timeout=1.0)
        self._synth_t = self._play_t = None

    def playing(self) -> bool:
        """True once the PlaybackStream is open (TTS audio on the wire)."""
        return self._pb is not None and not self._abort.is_set()

    def play_pcm(self, audio, src_rate: Optional[int] = None) -> None:
        """Play a raw float32 clip on the speech DAC (jingles). Stops any in-flight speech."""
        import numpy as np
        from reedus_companion.voice.tts import PlaybackStream
        self.stop()
        self.reset()
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if x.size == 0:
            return
        rate = int(src_rate or getattr(self.tts, "sample_rate", 24000) or 24000)
        pb = PlaybackStream(rate, device=config.TTS_DEVICE)
        try:
            pb.write(x)
            pb.pad(0.05)
        finally:
            pb.close()
        time.sleep(0.05)

    # --- finishing ---
    def finish(self, final_text: Optional[str] = None, timeout: float = 120.0) -> bool:
        """Flush the tail and wait for playback."""
        if self._abort.is_set():
            self.reset()
            return False
        tail = self._buf.strip()
        self._buf = ""

        if not self._streamed_any and final_text:
            self._enqueue(final_text)
        elif tail:
            self._enqueue(tail, from_stream=self._streamed_any)

        # generation is done, safe to run kokoro now
        self._flush_deferred()

        if self._synth_t is None:
            return False
        self._text_q.put(None)              # synth drains then forwards None to the player
        self._synth_t.join(timeout=timeout)
        if self._play_t is not None:
            self._play_t.join(timeout=timeout)
        aborted = self._abort.is_set()
        self._synth_t = self._play_t = None
        return not aborted

    def reset(self) -> None:
        """Clear state between turns. Call before feeding a new reply."""
        self._buf = ""
        self._deferred.clear()
        self._spoken.clear()
        self._streamed_any = False
        self._error = None
        self._abort = threading.Event()
        self._pb = None
        self._synth_t = self._play_t = None
        self._text_q = queue.Queue()
        self._audio_q = queue.Queue(maxsize=4)
        with self._pending_lock:
            self._pending = 0
            self._quiet_at = 0.0
