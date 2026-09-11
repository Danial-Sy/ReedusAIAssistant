"""cloud mode selftest: keys, one gemini turn, one inworld line."""
from __future__ import annotations

import os
import sys
import time

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


def _line(status: str, label: str, detail: str = "") -> None:
    print(f"[{status}] {label}" + (f"  --  {detail}" if detail else ""), flush=True)


def check_gemini() -> bool:
    print("\n=== Gemini ===")
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        _line(BAD, "GEMINI_API_KEY", "not set")
        return False
    try:
        from reedus_companion.cloud.gemini_backend import GeminiBackend
        be = GeminiBackend()
    except Exception as e:
        _line(BAD, "construct backend", f"{type(e).__name__}: {e}")
        return False
    _line(OK, "construct backend", be.model)
    try:
        import inspect
        from google import genai
        sig = inspect.signature(be._client.interactions.create)
        names = [n for n in sig.parameters if n not in ("self",)]
        _line(OK, "SDK", f"google-genai {getattr(genai, '__version__', '?')}")
        _line(OK, "create() accepts", ", ".join(names[:14]) + ("…" if len(names) > 14 else ""))
        from google.genai._gaos.types.interactions import createmodelinteraction as cmi
        for cls_name in dir(cmi):
            cls = getattr(cmi, cls_name)
            fields = getattr(cls, "model_fields", None)
            if isinstance(fields, dict) and "input" in fields:
                _line(OK, f"{cls_name} fields", ", ".join(sorted(fields)))
    except Exception as e:
        _line(WARN, "signature probe", f"{type(e).__name__}: {e}")

    ok = True
    # 1. single-turn, non-streaming
    try:
        t0 = time.perf_counter()
        out = be.chat([{"role": "system", "content": "Answer in one word."},
                       {"role": "user", "content": "Say hello."}], max_tokens=64)
        _line(OK, "single turn", f"{time.perf_counter() - t0:.2f}s -> {out[:40]!r}")
    except Exception as e:
        _line(BAD, "single turn", f"{type(e).__name__}: {e}")
        ok = False

    style_worked = None
    for style in ("structured", "flat"):
        os.environ["REEDUS_GEMINI_INPUT_STYLE"] = style
        be._input_style = style
        try:
            out = be.chat([{"role": "system", "content": "Answer in one word."},
                           {"role": "user", "content": "My name is Danial."},
                           {"role": "assistant", "content": "Noted."},
                           {"role": "user", "content": "What is my name?"}],
                          max_tokens=64)
            good = "danial" in out.lower()
            _line(OK if good else WARN, f"multi-turn ({style})",
                  f"-> {out[:40]!r}" + ("" if good else "  did not recall the name"))
            if good and style_worked is None:
                style_worked = style
        except Exception as e:
            _line(WARN, f"multi-turn ({style})", f"{type(e).__name__}: {e}")
    if style_worked:
        _line(OK, "PIN THIS", f"export REEDUS_GEMINI_INPUT_STYLE={style_worked}")
    else:
        _line(BAD, "multi-turn", "neither input shape carried history, "
                                 "fix _to_input in gemini_backend.py")
        ok = False
    os.environ.pop("REEDUS_GEMINI_INPUT_STYLE", None)
    be._input_style = style_worked or "auto"

    try:
        from reedus_companion.cloud.gemini_backend import create_interaction
        raw = {}
        marks = []
        t0 = time.perf_counter()
        stream = create_interaction(
            be._client, stream=True,
            model=be.model,
            input="List the numbers one through twenty, one per line, "
                  "words not digits.",
            generation_config={"thinking_level": "low", "max_output_tokens": 800},
        )
        for ev in stream:
            et = getattr(ev, "event_type", "?")
            dt = getattr(getattr(ev, "delta", None), "type", None)
            key = f"{et}" + (f"/{dt}" if dt else "")
            raw[key] = raw.get(key, 0) + 1
            if et == "step.delta" and dt == "text":
                marks.append((time.perf_counter() - t0,
                              getattr(ev.delta, "text", "") or ""))
        total = time.perf_counter() - t0
        _line(OK, "raw events", "  ".join(f"{k}={v}" for k, v in sorted(raw.items())))
        if not marks:
            _line(BAD, "streaming", "no text deltas at all"); ok = False
        else:
            first, last = marks[0][0], marks[-1][0]
            chars = sum(len(t) for _, t in marks)
            _line(OK, "streaming",
                  f"{len(marks)} text deltas, {chars} chars | first {first:.2f}s, "
                  f"last {last:.2f}s, total {total:.2f}s")
            if last - first < 0.25 and first > total * 0.5:
                _line(WARN, "  diagnosis",
                      f"text was generated in one burst after {first:.2f}s of "
                      f"thinking ({first / total * 100:.0f}% of the turn). "
                      f"Streaming is working; there is simply nothing to stream "
                      f"until thinking ends.")
                _line(WARN, "  lever",
                      "gemini-3.7-flash has no thinking level below 'low'. "
                      "gemini-3.6-flash accepts 'minimal' at the SAME price "
                      "($0.75/$3.75), worth an A/B: "
                      "REEDUS_GEMINI_MODEL=gemini-3.6-flash "
                      "REEDUS_GEMINI_THINK=minimal")
    except Exception as e:
        _line(BAD, "streaming", f"{type(e).__name__}: {e}")
        ok = False

    try:
        from reedus_companion.cloud import gemini_search
        if not gemini_search.available():
            _line(BAD, "search dispatch",
                  "gemini_search.available() is False, cloud mode would use "
                  "SearXNG. Is REEDUS_MODE=cloud set for this run?")
            ok = False
        else:
            q = "What was the top story on the BBC News front page today?"
            block = gemini_search.search(q)
            cites = block.count("\n") - 1
            if block.startswith("[web_search"):
                _line(BAD, "grounded search", block[:110]); ok = False
            elif cites <= 0:
                _line(BAD, "grounded search",
                      "answered with NO citations, Search did not actually run, "
                      "the model replied from priors. Cloud mode would be "
                      "answering current-events questions from stale memory.")
                _line(WARN, "  returned", block[:140].replace("\n", " | "))
                ok = False
            else:
                _line(OK, "grounded search", f"{cites} citation(s)")
                _line(OK, "  returned", block[:140].replace("\n", " | "))
    except Exception as e:
        _line(BAD, "grounded search", f"{type(e).__name__}: {e}")
        ok = False
    return ok


def check_inworld() -> bool:
    print("\n=== Inworld ===")
    if not (os.environ.get("INWORLD_API_KEY") or "").strip():
        _line(BAD, "INWORLD_API_KEY", "not set")
        return False
    try:
        from reedus_companion.voice.tts_inworld import InworldTTS
        tts = InworldTTS()
    except Exception as e:
        _line(BAD, "construct voice", f"{type(e).__name__}: {e}")
        return False
    _line(OK, "construct voice", f"{tts.model} / {tts.voice}")

    # short on purpose, every run bills against the one-time free allotment. REEDUS_SELFTEST_LINE.
    line = os.environ.get("REEDUS_SELFTEST_LINE", "Cloud voice check.")
    try:
        t0 = time.perf_counter()
        audio = tts.synth(line)
        el = time.perf_counter() - t0
    except Exception as e:
        _line(BAD, "synthesis", f"{type(e).__name__}: {e}")
        return False

    secs = audio.size / float(tts.sample_rate)
    _line(OK, "synthesis", f"{el:.2f}s for {secs:.1f}s of audio "
                           f"({secs / el:.1f}x realtime)")
    _line(OK if tts._steer else WARN, "steering",
          "accepted" if tts._steer else "REJECTED, voice will be flat, "
                                        "fix the field name in _payload")
    peak = float(abs(audio).max()) if audio.size else 0.0
    _line(OK if 0.05 < peak <= 1.0 else BAD, "audio sanity",
          f"{audio.size} samples @ {tts.sample_rate} Hz, peak {peak:.2f}")

    if os.environ.get("REEDUS_SELFTEST_WAV") == "1":
        out = "/tmp/reedus_cloud_voice.wav"
        try:
            tts.to_wav(line, out)
            _line(OK, "wrote sample", out)
        except Exception as e:
            _line(WARN, "wrote sample", f"{type(e).__name__}: {e}")
    return True


def main() -> int:
    print("Reedus cloud selftest")
    from reedus_companion.voice import voice_prefs
    _line(OK, "voice prefs", voice_prefs.describe())

    good = check_gemini()
    good = check_inworld() and good

    print("\n=== Spend ===")
    from reedus_companion.cloud import budget
    print(budget.summary())

    print("\n" + ("All good, cloud mode is ready." if good else
                  "Something above needs fixing before cloud mode will work."))
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
