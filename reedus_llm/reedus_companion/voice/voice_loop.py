"""the hands-free voice loop: wake word -> listen -> think -> speak, with barge-in."""
from __future__ import annotations
import argparse
import os
import threading
import time
from typing import Callable, List, Optional

from reedus_companion import config
from reedus_companion.serve.core import Reedus
from reedus_companion import mode as mode_mod
from reedus_companion.voice import events

DIM, RESET = "\033[2m", "\033[0m"

_THINK_CANCEL = "think_cancel"


class ThinkCancel:
    """Think-barge sentinel that also carries what the user said while interrupting."""
    __slots__ = ("audio",)

    def __init__(self, audio=None):
        self.audio = audio


def _is_think_cancel(barge) -> bool:
    if isinstance(barge, ThinkCancel):
        return True
    return isinstance(barge, str) and barge == _THINK_CANCEL


def _think_cancel_audio(barge):
    """Mic samples captured during a think-barge, or None."""
    audio = getattr(barge, "audio", None)
    if audio is None:
        return None
    try:
        return audio if int(getattr(audio, "size", 0) or 0) > 0 else None
    except Exception:
        return None


def _has_barge_audio(barge) -> bool:
    """Speak-barge captured mic samples."""
    if barge is None or _is_think_cancel(barge):
        return False
    try:
        return int(getattr(barge, "size", 0) or 0) > 0
    except Exception:
        return False


def record_until_silence(sample_rate=None, silence_sec=None, max_sec=None,
                         threshold=None):
    """Block, record from mic, stop after trailing silence. Returns float32 mono @ sample_rate."""
    import numpy as np
    import sounddevice as sd

    sample_rate = sample_rate or config.MIC_SAMPLE_RATE
    silence_sec = silence_sec if silence_sec is not None else config.MIC_SILENCE_SEC
    max_sec = max_sec if max_sec is not None else config.MIC_MAX_SEC
    threshold = threshold if threshold is not None else float(
        getattr(config, "MIC_RMS", 0.012))
    hangover = float(getattr(config, "MIC_HANGOVER", 0.28))

    block = int(sample_rate * 0.05)
    silent_needed = max(1, int(silence_sec / 0.05))
    max_blocks = int(max_sec / 0.05)

    frames, silent_run, started, count = [], 0, False, 0
    speech_peak = threshold
    with sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32", blocksize=block,
                        device=config.MIC_DEVICE) as stream:
        while count < max_blocks:
            data, _ = stream.read(block)
            mono = data[:, 0]
            frames.append(mono.copy())
            level = float(np.sqrt(np.mean(mono ** 2)))
            if level > threshold:
                started, silent_run = True, 0
                speech_peak = max(speech_peak * 0.97, level)
            elif started:
                end_thresh = max(threshold * 0.55, speech_peak * hangover)
                if level < end_thresh:
                    silent_run += 1
                    if silent_run >= silent_needed:
                        break
                else:
                    silent_run = 0
                speech_peak = max(speech_peak * 0.97, threshold)
            count += 1
    if not frames:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(frames)


def _wire_status(reedus, speaker) -> None:
    """Speak-first acks ('gimme a sec') so search/corpus/look turns don't feel crashed."""
    def _status(line: str) -> None:
        print(f"{DIM}  … {line}{RESET}", flush=True)
        if speaker is not None:
            try:
                speaker.say(line)
            except Exception:
                pass
    reedus.on_status = _status


def build_backend(name, args):
    if name == "gemini":
        from reedus_companion.cloud.fallback import FallbackBackend
        from reedus_companion.cloud.gemini_backend import GeminiBackend

        def _local():
            """Built only if the cloud dies mid-conversation."""
            from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
            return LlamaCppBackend(gguf_path=args.gguf, router_gguf=args.router_gguf)

        return FallbackBackend(GeminiBackend(), _local,
                               announce=getattr(args, "_announce", None))
    if name == "hf":
        from reedus_companion.serve.backend_hf import HFBackend
        return HFBackend(adapter_dir=args.adapter, merged_dir=args.merged)
    if name == "server":
        from reedus_companion.serve.backend_server import ServerBackend
        return ServerBackend(main_url=getattr(args, "main_url", None),
                             router_url=getattr(args, "router_url", None))
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
    return LlamaCppBackend(gguf_path=args.gguf, router_gguf=args.router_gguf)


def _release_speaker(speaker) -> None:
    """Free the USB DAC so a jingle can open the output stream."""
    if speaker is None:
        return
    try:
        speaker.stop()
    except Exception:
        pass
    try:
        speaker.reset()
    except Exception:
        pass
    time.sleep(0.05)


def _play_ui(kind: str, enabled: bool, speaker=None) -> None:
    if not enabled or not kind:
        return
    try:
        from reedus_companion.voice.jingle import make_jingle
        audio = make_jingle(kind)
        if speaker is not None and hasattr(speaker, "play_pcm"):
            speaker.play_pcm(audio, src_rate=24000)
        else:
            _release_speaker(speaker)
            from reedus_companion.voice.jingle import play_jingle
            play_jingle(kind)
    except Exception as e:                                      # noqa: BLE001
        print(f"{DIM}  [jingle {kind} failed: {e}]{RESET}", flush=True)


def _profile_backend(backend: str):
    """The PROFILE list for the active backend, or None."""
    if backend == "gguf":
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        return LlamaCppBackend.PROFILE
    if backend == "server":
        from reedus_companion.serve.backend_server import ServerBackend
        return ServerBackend.PROFILE
    return None


def _do_turn(reedus, speaker, text: str, backend: str, on_token) -> tuple[str, float, float, object]:
    """Serialised entry point: a typed turn from ReedOS and a spoken turn share one lock."""
    lock = _RUNTIME.get("turn_lock")
    if lock is None:
        return _do_turn_inner(reedus, speaker, text, backend, on_token)
    with lock:
        _RUNTIME["busy"] = "voice"
        try:
            return _do_turn_inner(reedus, speaker, text, backend, on_token)
        finally:
            _RUNTIME["busy"] = None


def _do_turn_inner(reedus, speaker, text: str, backend: str, on_token) -> tuple[str, float, float, object]:
    """One brain turn. Returns (reply, think_s, speak_s, barge_audio|None)."""
    import threading
    prof = _profile_backend(backend)
    if prof is not None:
        prof.clear()
    if speaker is not None:
        speaker.reset()
    reedus.on_token = on_token if speaker is not None else None
    events.emit("heard", text=text)
    events.emit("state", state="thinking")
    cancel = threading.Event()
    reedus._cancel = cancel
    from reedus_companion.voice.barge import ThinkCancelWatcher
    think_w = ThinkCancelWatcher(
        cancel,
        speaker=speaker,
        on_hit=lambda: (
            print(f"{DIM}  [interrupted think, listening]{RESET}", flush=True),
            _release_speaker(speaker),
            events.emit("wake"),
        ),
    )
    think_w.start()
    t1 = time.perf_counter()
    try:
        reply = reedus.reply(text)
    finally:
        think_w.stop()
        reedus._cancel = None
    think_s = time.perf_counter() - t1
    if reedus.cancelled or cancel.is_set():
        _release_speaker(speaker)
        print(f"{DIM}  (generation cancelled){RESET}")
        events.emit("state", state="listening")
        return "", think_s, 0.0, ThinkCancel(getattr(think_w, "audio", None))

    print(f"reedus> {reply}")
    events.emit("reply", text=reply)
    speak_s = 0.0
    barge_audio = None
    if speaker is not None:
        from reedus_companion.voice.barge import BargeWatcher
        watcher = BargeWatcher(speaker, on_barge=lambda: (
            print(f"{DIM}  [interrupted, listening]{RESET}", flush=True),
            events.emit("wake")))
        watcher.start()
        events.emit("state", state="speaking")
        t2 = time.perf_counter()
        speaker.finish(reply)
        speak_s = time.perf_counter() - t2
        barge_audio = watcher.stop()
    if prof is not None:
        for r in prof:
            name = r.get("name") or "?"
            ttft = r.get("ttft_s") or 0.0
            gen_s = r.get("gen_tok_s") or 0.0
            extra = f"  {ttft:.2f}s pre" if ttft else ""
            if gen_s:
                extra += f"  {gen_s:.1f} tok/s"
            if r.get("cancelled"):
                extra += "  CANCELLED"
            print(f"{DIM}      {name:<16} {r.get('wall_s', 0):6.2f}s  "
                  f"{r.get('tier') or '':<6}{extra}{RESET}")
    return reply, think_s, speak_s, barge_audio


def run_always_on(reedus, stt, speaker, backend: str, hear: Callable,
                  jingled: bool = False) -> None:
    """Original loop: every utterance is a turn."""
    def _on_token(piece: str) -> None:
        if speaker is not None:
            speaker.feed(piece)

    print("\nReedus is listening. Just talk. (Ctrl-C to quit)\n")
    while True:
        speaker, backend = _apply_pending(reedus, speaker, backend)
        events.emit("state", state="listening")
        audio = hear()
        if getattr(audio, "size", 1) == 0:
            continue
        t0 = time.perf_counter()
        text = stt.transcribe(audio) if not isinstance(audio, str) else audio
        stt_s = time.perf_counter() - t0
        if isinstance(audio, str):
            stt_s = 0.0
        if not (text or "").strip():
            continue
        print(f"you   > {text}")
        _want = mode_mod.switch_request(text)
        if _want:
            speaker, backend = _live_switch(reedus, speaker, backend, _want)
            continue
        _play_ui("listen", jingled, speaker)
        reply, think_s, speak_s, _barge = _do_turn(reedus, speaker, text, backend, _on_token)
        wait_s = stt_s + think_s
        print(f"{DIM}  [wait {wait_s:.1f}s  stt {stt_s:.1f}s  think {think_s:.1f}s  "
              f"| speaking {speak_s:.1f}s]{RESET}")
        _play_ui("ready", jingled, speaker)
        print()


def _consume_interruption(reedus, speaker, stt, session, backend: str, on_token,
                          audio, jingled: bool, label: str,
                          pop_assistant: bool) -> str:
    """Transcribe an interruption and run it as the next turn. The listen jingle fires FIRST, before STT."""
    _play_ui("listen", jingled, speaker)
    t_b = time.perf_counter()
    btext = (stt.transcribe(audio) or "").strip()
    if not btext:
        print(f"{DIM}  (false {label}, ignoring){RESET}")
        return "done"
    print(f"you   > {btext}  {DIM}({label}){RESET}")
    if pop_assistant:
        try:
            if reedus.history and reedus.history[-1].get("role") == "assistant":
                reedus.history.pop()
        except Exception:
            pass
    bev = session.on_transcript(btext)
    if bev.action == "stop":
        events.emit("session_end")
        print(f"{DIM}  [{bev.note}]{RESET}")
        _play_ui(bev.jingle, jingled, speaker)
        print(f"{DIM}  asleep again. Say Hey Reedus when you want me back.{RESET}\n")
        return "sleep"
    if bev.action in ("arm", "ignore"):
        if bev.action == "arm":
            print(f"{DIM}  [{bev.note}]{RESET}")
            if bev.jingle and bev.jingle != "listen":
                _play_ui(bev.jingle, jingled, speaker)
        print()
        return "sleep"
    if bev.action == "turn" and bev.text:
        _reply, think_s, speak_s, again = _do_turn(
            reedus, speaker, bev.text, backend, on_token)
        stt_b = time.perf_counter() - t_b
        print(f"{DIM}  [{label}-stt {stt_b:.1f}s  think {think_s:.1f}s  "
              f"| speaking {speak_s:.1f}s]{RESET}")
        if _is_think_cancel(again):
            _play_ui("listen", jingled, speaker)
            print()
            return "sleep"
    return "done"


def _wake_listener():
    """WakeListener when a wake-word model is loaded, else None (keep the old idle path)."""
    try:
        from reedus_companion.voice.listener import WakeListener
        wl = WakeListener()
        return wl if wl.available else None
    except Exception as e:                                       # noqa: BLE001
        print(f"{DIM}  [wake-word listener unavailable: {type(e).__name__}: {e}]{RESET}")
        return None


def run_wake_loop(reedus, stt, speaker, backend: str, hear: Callable,
                  jingled: bool = True, max_empty: int = 3) -> None:
    """Idle until wake word; session until stop / silence timeout."""
    from reedus_companion.voice.session import WakeSession

    def _on_token(piece: str) -> None:
        if speaker is not None:
            speaker.feed(piece)

    session = WakeSession(max_empty=max_empty)
    wl = _wake_listener()
    if wl is not None:
        print(f"{DIM}  [idle wake: {wl.det.describe()}]{RESET}")
    else:
        from reedus_companion.voice.wakeword import shared_detector
        print(f"{DIM}  [idle wake: transcript gate, {shared_detector().error}]{RESET}")
    print("\nReedus is asleep. Say \"Hey Reedus\" to wake. "
          "\"Goodbye\" to sleep. (Ctrl-C to quit)\n")
    _play_ui("ready", jingled, speaker)
    while True:
        speaker, backend = _apply_pending(reedus, speaker, backend)
        state = "SESSION" if session.active else "IDLE"
        print(f"{DIM}  … {state}{RESET}", flush=True)
        events.emit("state", state="listening" if session.active else "idle")
        was_active = session.active
        # idle + a wake model: nothing is transcribed until the name is said, so a room full
        # of talking costs ~5% of a core instead of a whisper pass per noise. the name is
        # re-attached as text so the session state machine runs unchanged.
        heard_name = False
        if wl is not None and not session.active:
            audio = wl.wait_and_capture()
            heard_name = audio is not None
            if audio is None:
                continue
        else:
            audio = hear()
        t0 = time.perf_counter()
        if isinstance(audio, str):
            text = audio
            stt_s = 0.0
        else:
            if getattr(audio, "size", 0) == 0 and heard_name:
                text, stt_s = "Reedus", 0.0
            elif getattr(audio, "size", 0) == 0:
                ev = session.on_transcript("")
                if ev.action == "stop":
                    events.emit("session_end")
                    print(f"{DIM}  [{ev.note}]{RESET}")
                    _play_ui(ev.jingle, jingled, speaker)
                    print(f"{DIM}  asleep again.{RESET}\n")
                continue
            else:
                text = stt.transcribe(audio)
                stt_s = time.perf_counter() - t0
                if heard_name:
                    text = f"Reedus {(text or '').strip()}".strip()
        if (text or "").strip():
            # idle: don't print pre-wake chatter as if it were a command
            if not session.active:
                from reedus_companion.voice.wake import from_wake_onward, has_wake
                if has_wake(text):
                    gated = from_wake_onward(text)
                    if gated and gated != (text or "").strip():
                        print(f"you   > {gated}  {DIM}(ignored talk before wake){RESET}")
                    else:
                        print(f"you   > {text}")
                else:
                    print(f"you   > {text}")
            else:
                print(f"you   > {text}")

        ev = session.on_transcript(text or "")
        if session.active and not was_active:
            events.emit("wake")
        if ev.action == "stop":
            events.emit("session_end")
        if ev.action == "ignore":
            if ev.note == "no wake":
                print(f"{DIM}  (ignored, say Hey Reedus){RESET}\n")
            continue

        if ev.action == "arm":
            print(f"{DIM}  [{ev.note}]{RESET}")
            _play_ui(ev.jingle, jingled, speaker)
            print()
            continue

        if ev.action == "stop":
            print(f"{DIM}  [{ev.note}]{RESET}")
            _play_ui(ev.jingle, jingled, speaker)
            print(f"{DIM}  asleep again. Say Hey Reedus when you want me back.{RESET}\n")
            continue

        if ev.action == "turn":
            if ev.jingle:
                _play_ui(ev.jingle, jingled, speaker)
            if ev.text != (text or "").strip():
                print(f"{DIM}  (wake stripped → {ev.text!r}){RESET}")
            _want = mode_mod.switch_request(ev.text)
            if _want:
                speaker, backend = _live_switch(reedus, speaker, backend, _want)
                print()
                continue
            reply, think_s, speak_s, barge_audio = _do_turn(
                reedus, speaker, ev.text, backend, _on_token)
            wait_s = stt_s + think_s
            print(f"{DIM}  [wait {wait_s:.1f}s  stt {stt_s:.1f}s  think {think_s:.1f}s  "
                  f"| speaking {speak_s:.1f}s]{RESET}")
            if _is_think_cancel(barge_audio):
                captured = _think_cancel_audio(barge_audio)
                if captured is None:
                    _play_ui("listen", jingled, speaker)
                    print()
                    continue
                if _consume_interruption(reedus, speaker, stt, session, backend, _on_token,
                                         captured, jingled, "interrupted",
                                         pop_assistant=False) == "sleep":
                    continue
                post = session.after_reply()
                _play_ui(post.jingle, jingled, speaker)
                print()
                continue
            if _has_barge_audio(barge_audio):
                if _consume_interruption(reedus, speaker, stt, session, backend, _on_token,
                                         barge_audio, jingled, "barge",
                                         pop_assistant=True) == "sleep":
                    continue
            post = session.after_reply()
            _play_ui(post.jingle, jingled, speaker)
            print()


_RUNTIME: dict = {
    "args": None,
    "local_backend": "gguf",
    "reedus": None,
    "speaker": None,
    "backend": None,
    "turn_lock": None,
    "pending_mode": None,     # set by the API, consumed by the loop thread
    "switching": False,
    "busy": None,
}


def _publish(reedus, speaker, backend_name: str) -> None:
    """Make the loop's current handles visible to the control API."""
    _RUNTIME["reedus"] = reedus
    _RUNTIME["speaker"] = speaker
    _RUNTIME["backend"] = backend_name


def _apply_pending(reedus, speaker, backend_name: str):
    """Run a mode switch the UI asked for, on this thread."""
    want = _RUNTIME.get("pending_mode")
    if want:
        _RUNTIME["pending_mode"] = None
        if want != mode_mod.current():
            _RUNTIME["switching"] = True
            try:
                speaker, backend_name = _live_switch(reedus, speaker, backend_name, want)
            finally:
                _RUNTIME["switching"] = False
    _publish(reedus, speaker, backend_name)
    return speaker, backend_name


def _say(speaker, line: str, wait: float = 30.0) -> None:
    """Speak and wait. Anything that tears down the audio device must finish first."""
    print(f"reedus> {line}")
    if speaker is None:
        return
    try:
        speaker.say(line)
        speaker.finish(timeout=wait)
    except Exception:
        pass


def _swap_voice(speaker, target: str):
    """Rebuild the StreamSpeaker around the voice `target` mode wants."""
    if speaker is None:
        return None
    from reedus_companion.serve.core import scrub
    from reedus_companion.voice.stream_speaker import StreamSpeaker

    if target == mode_mod.CLOUD:
        from reedus_companion.cloud import CloudUnavailable
        from reedus_companion.cloud.fallback import FallbackTTS
        try:
            from reedus_companion.voice.tts_inworld import InworldTTS
            new_tts = InworldTTS()
            new_tts.warm()
        except CloudUnavailable as e:
            print(f"[reedus] cloud voice unavailable ({e}), keeping current voice.")
            return speaker

        def _local_voice():
            from reedus_companion.voice.tts import TTS
            return TTS()

        wrapped = FallbackTTS(new_tts, _local_voice)
    else:
        from reedus_companion.voice.tts import TTS
        wrapped = TTS()
        wrapped.warm()

    _release_speaker(speaker)
    try:
        speaker.stop()
    except Exception:
        pass
    return StreamSpeaker(wrapped, debug=False, scrub=scrub)


def _live_switch(reedus, speaker, backend_name: str, target: str):
    """Change mode mid-conversation. Returns (speaker, backend_name)."""
    current = mode_mod.current()
    if target == current:
        _say(speaker, f"Already in {current} mode.")
        return speaker, backend_name

    ok, why = mode_mod.preflight(target)
    if not ok:
        _say(speaker, f"{why} Staying in {current} mode.")
        return speaker, backend_name

    _say(speaker, f"You got it, {target} mode. Give me a second.")
    mode_mod.set_current(target)
    new_name = "gemini" if target == mode_mod.CLOUD else _RUNTIME["local_backend"]

    old_backend = getattr(reedus, "backend", None)
    try:
        reedus.backend = build_backend(new_name, _RUNTIME["args"])
    except Exception as e:                                       # noqa: BLE001
        mode_mod.set_current(current)
        print(f"[reedus] mode switch failed: {type(e).__name__}: {e}")
        _say(speaker, "That didn't take. Staying where I am.")
        return speaker, backend_name
    try:
        if old_backend is not None:
            old_backend.close()
    except Exception:
        pass

    speaker = _swap_voice(speaker, target)
    _RUNTIME["args"]._announce = (
        (lambda line: speaker.say(line)) if speaker is not None else None)
    _wire_status(reedus, speaker)
    try:
        print("Warming…", end=" ", flush=True)
        print(f"{reedus.warm():.1f}s")
    except Exception:
        pass
    _say(speaker, f"Right, {target} mode. Go ahead.")
    return speaker, new_name


def _choose_mode(args, speaker, stt, hear) -> str:
    """Spoken 'local or cloud?' unless --mode / REEDUS_MODE already decided it."""
    forced = getattr(args, "mode", None)
    if forced in (mode_mod.LOCAL, mode_mod.CLOUD):
        chosen = mode_mod.set_current(forced)
        print(f"[reedus] mode: {chosen} (--mode)")
    elif forced == "ask":
        os.environ.pop("REEDUS_MODE", None)
        chosen = mode_mod.ask(speaker, stt, hear)
    else:
        chosen = mode_mod.ask(speaker, stt, hear)

    ok, why = mode_mod.preflight(chosen)
    if not ok:
        line = f"{why} Staying local."
        print(f"[reedus] cloud preflight failed: {why}")
        try:
            if speaker is not None:
                speaker.say(line)
                speaker.finish(timeout=20.0)
        except Exception:
            pass
        chosen = mode_mod.set_current(mode_mod.LOCAL)
    print(f"[reedus] === {chosen.upper()} MODE ===")
    return chosen


def _swap_to_cloud_voice(speaker):
    """Replace the Kokoro speaker with an Inworld one."""
    from reedus_companion.cloud import CloudUnavailable
    from reedus_companion.cloud.fallback import FallbackTTS
    from reedus_companion.serve.core import scrub
    from reedus_companion.voice.stream_speaker import StreamSpeaker

    try:
        from reedus_companion.voice.tts_inworld import InworldTTS
        cloud_tts = InworldTTS()
        print("Warming cloud voice…", end=" ", flush=True)
        t0 = time.perf_counter()
        cloud_tts.warm()
        print(f"{time.perf_counter() - t0:.1f}s")
    except CloudUnavailable as e:
        print(f"[reedus] cloud voice unavailable ({e}), keeping Kokoro.", flush=True)
        return speaker

    _release_speaker(speaker)
    try:
        speaker.stop()
    except Exception:
        pass

    def _local_voice():
        from reedus_companion.voice.tts import TTS
        return TTS()

    return StreamSpeaker(FallbackTTS(cloud_tts, _local_voice),
                         debug=False, scrub=scrub)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "gguf", "server", "gemini"], default="gguf",
                    help="gguf=in-process llama-cpp-python; server=native llama-server HTTP; "
                         "gemini=cloud mode (implied by --mode cloud)")
    ap.add_argument("--mode", choices=["local", "cloud", "ask"], default=None,
                    help="skip the spoken 'local or cloud?' prompt. Default: ask "
                         "(or REEDUS_MODE if set)")
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--gguf", default=None)
    ap.add_argument("--router-gguf", default=None,
                    help="small GGUF for router/classifier calls (gguf backend)")
    ap.add_argument("--main-url", default=None,
                    help="llama-server URL for persona (server backend; else REEDUS_MAIN_URL)")
    ap.add_argument("--router-url", default=None,
                    help="llama-server URL for router (server backend; else REEDUS_ROUTER_URL)")
    ap.add_argument("--no-speak", action="store_true", help="print replies instead of speaking")
    ap.add_argument("--cam", default=None,
                    help="camera source for vision: webcam index ('0') or phone URL. Sets REEDUS_CAM.")
    ap.add_argument("--no-vision", action="store_true", help="disable the camera/vision path")
    ap.add_argument("--wake", action="store_true",
                    help="Alexa-style: idle until 'Hey Reedus', sleep on goodbye")
    ap.add_argument("--jingle", action="store_true", default=None,
                    help="play listen/ready/sleep tones (default on with --wake)")
    ap.add_argument("--no-jingle", action="store_true", help="disable UI tones")
    ap.add_argument("--simulate", nargs="*", default=None,
                    help="scripted transcripts instead of the mic (pipeline test)")
    ap.add_argument("--max-empty", type=int, default=3,
                    help="in --wake, consecutive empty captures before auto-sleep")
    ap.add_argument("--no-api", action="store_true",
                    help="don't open the ReedOS control API (serve/control_api.py, :8766)")
    args = ap.parse_args()

    if args.cam is not None:
        os.environ["REEDUS_CAM"] = args.cam
    if args.no_vision:
        os.environ["REEDUS_VISION"] = "0"
    os.environ.setdefault("REEDUS_DEBUG", "1")
    os.environ.setdefault("REEDUS_PROFILE", "1")

    jingled = False
    if args.no_jingle:
        jingled = False
    elif args.jingle:
        jingled = True
    elif args.wake:
        jingled = True

    simulate: Optional[List[str]] = args.simulate
    if simulate is not None and len(simulate) == 0:
        # default demo script when --simulate is passed with no lines
        simulate = [
            "hey there what's up",                 # ignored (no wake)
            "Hey Reedus",                          # arm
            "what's going on",                     # turn
            "I'm just talking to you my guy",      # turn (chitchat)
            "goodbye",                             # sleep
            "still here?",                         # ignored
            "Hey Reedus, how was your day",        # wake+cmd
            "Reedus go to sleep",                  # sleep
        ]

    from reedus_companion.voice.stt import STT, canon_name

    class _SimSTT:
        """Pass-through that still runs canon_name (whisper miss -> Reedus)."""
        def transcribe(self, audio):
            if isinstance(audio, str):
                return canon_name(audio)
            return ""

    if simulate is not None:
        print("SIMULATE mode, no mic, scripted transcripts")
        stt = _SimSTT()
        queue = list(simulate)

        def hear():
            if not queue:
                raise KeyboardInterrupt
            return queue.pop(0)
        if os.environ.get("REEDUS_SIM_STUB", "0") == "1":
            class _Stub:
                def warm(self): return 0.0
                def reply(self, t): return f"(stub) heard: {t}"
                def close(self): pass
                on_token = None
            reedus = _Stub()
            speaker = None
            print("  stub brain (REEDUS_SIM_STUB=1)")
        else:
            _RUNTIME["args"] = args
            _RUNTIME["local_backend"] = args.backend
            if mode_mod.current() == mode_mod.CLOUD and \
                    mode_mod.preflight(mode_mod.CLOUD)[0]:
                args.backend = "gemini"
            print(f"Loading Reedus brain… ({mode_mod.current()} mode)")
            reedus = Reedus(build_backend(args.backend, args))
            print("Warming the prompt cache…", end=" ", flush=True)
            print(f"{reedus.warm():.1f}s")
            speaker = None
            if not args.no_speak:
                from reedus_companion.voice.tts import TTS
                from reedus_companion.voice.stream_speaker import StreamSpeaker
                from reedus_companion.serve.core import scrub
                print("Loading voice…")
                tts = TTS()
                tts.warm()
                speaker = StreamSpeaker(tts, debug=False, scrub=scrub)
            _wire_status(reedus, speaker)
    else:
        print("Loading ears (whisper)…")
        stt = STT()
        hear = record_until_silence

        speaker = None
        if not args.no_speak:
            from reedus_companion.voice.tts import TTS
            from reedus_companion.voice.stream_speaker import StreamSpeaker
            from reedus_companion.serve.core import scrub
            print("Loading voice…")
            boot_tts = TTS()
            print("Warming voice…", end=" ", flush=True)
            t0_tts = time.perf_counter()
            boot_tts.warm()
            print(f"{time.perf_counter() - t0_tts:.1f}s")
            speaker = StreamSpeaker(boot_tts, debug=False, scrub=scrub)

        _RUNTIME["args"] = args
        _RUNTIME["local_backend"] = args.backend      # before cloud overwrites it
        chosen = _choose_mode(args, speaker, stt, hear)
        if chosen == mode_mod.CLOUD:
            args.backend = "gemini"
            if speaker is not None:
                speaker = _swap_to_cloud_voice(speaker)

        args._announce = (lambda line: speaker.say(line)) if speaker is not None else None

        print("Loading Reedus brain…")
        reedus = Reedus(build_backend(args.backend, args))
        print("Warming the prompt cache…", end=" ", flush=True)
        print(f"{reedus.warm():.1f}s")
        _wire_status(reedus, speaker)

    _RUNTIME["turn_lock"] = threading.RLock()
    _publish(reedus, speaker, args.backend)
    api = None
    if not args.no_api and (os.environ.get("REEDUS_API") or "1") != "0":
        try:
            from reedus_companion.serve.control_api import VoiceLoopHooks
            from reedus_companion.serve.control_api import serve as _serve_api
            api, _ = _serve_api(VoiceLoopHooks(_RUNTIME))
        except Exception as e:                                   # noqa: BLE001
            print(f"[control-api] not started ({type(e).__name__}: {e}), voice unaffected")

    try:
        if args.wake or simulate is not None:
            run_wake_loop(reedus, stt, speaker, args.backend, hear,
                          jingled=jingled, max_empty=args.max_empty)
        else:
            run_always_on(reedus, stt, speaker, args.backend, hear, jingled=jingled)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        if api is not None:
            try:
                api.shutdown()
            except Exception:
                pass
        reedus.close()
        print("\nlater.")


if __name__ == "__main__":
    main()
