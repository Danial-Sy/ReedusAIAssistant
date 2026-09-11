"""interactive text chat, works with either backend."""
from __future__ import annotations
import argparse
import os
import sys
import time

from reedus_companion.serve.core import Reedus

DIM, RESET, BOLD = "\033[2m", "\033[0m", "\033[1m"


def build_backend(args):
    if args.backend == "gemini":
        from reedus_companion.cloud.gemini_backend import GeminiBackend
        return GeminiBackend()
    if args.backend == "hf":
        from reedus_companion.serve.backend_hf import HFBackend
        return HFBackend(adapter_dir=args.adapter, merged_dir=args.merged,
                         load_4bit=not args.full_precision, base_only=args.base_only)
    if args.backend == "server":
        from reedus_companion.serve.backend_server import ServerBackend
        return ServerBackend(main_url=args.main_url, router_url=args.router_url)
    from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
    return LlamaCppBackend(gguf_path=args.gguf, router_gguf=args.router_gguf)


def _profile_rows(args):
    """Per-call profile rows for the last turn, if the backend records them."""
    if args.backend == "gemini":
        from reedus_companion.cloud.gemini_backend import GeminiBackend
        return list(GeminiBackend.PROFILE)
    if args.backend == "gguf":
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        return list(LlamaCppBackend.PROFILE)
    if args.backend == "server":
        from reedus_companion.serve.backend_server import ServerBackend
        return list(ServerBackend.PROFILE)
    return []


_CALL_NAMES = [
    ("You are the search router", "search-router"),
    ("Decide whether answering", "search-router"),
    ("You decide if the user's LAST message", "look-router"),
    ("You read web search results", "extract"),
    ("From these search results", "topic-brief"),
    ("You decide whether a user is COMPLAINING", "correction"),
    ("Pull the TITLE", "topic-router"),
    ("Name the specific thing", "subjects"),
    ("Rewrite the message shorter", "condense"),
]


def _call_name(head: str) -> str:
    h = (head or "").strip()
    for prefix, name in _CALL_NAMES:
        if h.startswith(prefix):
            return name
    low = h.lower()
    if "condense" in low or "shorter" in low or "tighten" in low:
        return "condense"
    if "subject" in low or "who or what" in low:
        return "subjects"
    if "verify" in low or "draft" in low:
        return "draft-check"
    if "reedus" in low:
        return "persona"
    return h[:14] or "?"


def _clear_profile(args):
    if args.backend == "gguf":
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        LlamaCppBackend.PROFILE.clear()
    elif args.backend == "server":
        from reedus_companion.serve.backend_server import ServerBackend
        ServerBackend.PROFILE.clear()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "gguf", "server", "gemini"], default="gguf",
                    help="gemini = cloud mode (needs GEMINI_API_KEY)")
    ap.add_argument("--adapter", default=None, help="LoRA adapter dir (hf backend)")
    ap.add_argument("--merged", default=None, help="merged fp16 dir (hf backend)")
    ap.add_argument("--gguf", default=None, help="GGUF path (gguf backend)")
    ap.add_argument("--router-gguf", default=None,
                    help="small GGUF to serve the router/classifier calls (gguf backend). "
                         "Overrides REEDUS_ROUTER_MODEL; omit both for a single model.")
    ap.add_argument("--main-url", default=None,
                    help="llama-server URL for persona (server backend)")
    ap.add_argument("--router-url", default=None,
                    help="llama-server URL for router (server backend)")
    ap.add_argument("--full-precision", action="store_true", help="hf: load in bf16 not 4-bit")
    ap.add_argument("--base-only", action="store_true",
                    help="hf: load raw base model with NO adapter (diagnostic)")
    ap.add_argument("--once", default=None, help="single prompt then exit")
    ap.add_argument("--cam", default=None,
                    help="camera source for vision: webcam index ('0') or phone URL "
                         "('http://admin:admin@192.168.1.235:8081'). Sets REEDUS_CAM.")
    ap.add_argument("--no-vision", action="store_true", help="disable the camera/vision path")
    ap.add_argument("--no-warm", action="store_true",
                    help="skip cache priming, the first turn will be ~10s slower")
    ap.add_argument("--no-timing", action="store_true", help="hide the per-turn timing line")
    ap.add_argument("--no-stream", action="store_true", help="wait for the full reply")
    ap.add_argument("--voice", action="store_true",
                    help="speak replies. Streams sentence-by-sentence as they generate, so "
                         "he starts talking before the reply is finished.")
    args = ap.parse_args()

    if args.cam is not None:
        os.environ["REEDUS_CAM"] = args.cam
    if args.no_vision:
        os.environ["REEDUS_VISION"] = "0"
    show_timing = not args.no_timing
    if show_timing and args.backend in ("gguf", "server"):
        os.environ.setdefault("REEDUS_PROFILE", "1")

    print(f"Loading Reedus ({args.backend})…")
    t0 = time.time()
    reedus = Reedus(build_backend(args))
    print(f"  model loaded in {time.time()-t0:.1f}s")

    # prime the prompt cache so the first question doesn't pay ~10-15s of prefill
    if not args.no_warm:
        print("Warming the prompt cache…", end=" ", flush=True)
        print(f"{reedus.warm():.1f}s")
    print(f"\n{BOLD}Reedus is up.{RESET}  /help for commands\n")

    if args.once:
        print(reedus.reply(args.once))
        reedus.close()
        return

    speaker = None
    if args.voice:
        print("Loading voice…")
        from reedus_companion.voice.tts import TTS
        from reedus_companion.voice.stream_speaker import StreamSpeaker
        from reedus_companion.serve.core import scrub
        speaker = StreamSpeaker(TTS(), debug=True, scrub=scrub)
        print("  voice ready\n")

    # status lines the moment slow work starts, spoken not just printed
    def _status(line: str) -> None:
        print(f"{DIM}  … {line}{RESET}", flush=True)
        if speaker is not None:
            speaker.say(line)

    reedus.on_status = _status

    def _replaced(old: str, new: str) -> None:
        print(f"\n{DIM}  ^ corrected after verification{RESET}")
        if speaker is not None:
            speaker.say("actually, correction,")

    reedus.on_replaced = _replaced

    streamed: list[str] = []
    do_stream = not args.no_stream

    def _on_token(piece: str) -> None:
        streamed.append(piece)
        sys.stdout.write(piece)
        sys.stdout.flush()
        if speaker is not None:
            speaker.feed(piece)

    turns = 0
    total_s = 0.0
    try:
        while True:
            try:
                u = input(f"{BOLD}you >{RESET} ").strip()
            except EOFError:
                break
            if not u:
                continue

            low = u.lower()
            if low in ("/quit", "/exit"):
                break
            if low == "/help":
                print(__doc__.split("Slash commands:")[1]); continue
            if low == "/timing":
                show_timing = not show_timing
                print(f"  timing {'on' if show_timing else 'off'}\n"); continue
            if low == "/stream":
                do_stream = not do_stream
                print(f"  streaming {'on' if do_stream else 'off'}\n"); continue
            if low == "/reset":
                reedus.history.clear()
                print("  history cleared (saved memory untouched)\n"); continue
            if low == "/warm":
                print(f"  warmed in {reedus.warm():.1f}s\n"); continue
            if low == "/stats":
                print(f"  {turns} turns, {total_s:.1f}s total, "
                      f"{total_s/max(1,turns):.1f}s/turn")
                from reedus_companion import router_stats
                print(router_stats.summary() + "\n"); continue

            streamed.clear()
            _clear_profile(args)
            if speaker is not None:
                speaker.reset()
            reedus.on_token = _on_token if (do_stream or speaker is not None) else None

            t = time.time()
            print("reedus > ", end="", flush=True)
            answer = reedus.reply(u)
            elapsed = time.time() - t
            turns += 1
            total_s += elapsed

            raw = "".join(streamed).strip()
            trivial = raw.lower().split() == answer.lower().split()
            if raw and raw != answer and not trivial:
                print(f"\n{DIM}  ^ raw draft; final differs:{RESET}")
                print(f"reedus > {answer}")
            elif not raw:
                print(answer)
            else:
                print()

            if speaker is not None:
                speaker.finish(answer)

            if show_timing:
                rows = _profile_rows(args)
                if rows:
                    p = sum(r["prompt_tokens"] for r in rows)
                    g = sum(r["completion_tokens"] for r in rows)
                    inf = sum(r["wall_s"] for r in rows)
                    print(f"{DIM}  [{elapsed:.1f}s · {len(rows)} call{'s' if len(rows)!=1 else ''}"
                          f" · {p} prompt / {g} gen tok · {inf/elapsed*100:.0f}% in model]{RESET}")
                    # per-call rows: which call is slow is the question, not the total. TIER catches a
                    # persona call quietly served by the 1.7B, which reads as good english that isn't reedus.
                    split_on = any(r.get("tier") == "router" for r in rows)
                    for r in rows:
                        gen, wall = r["completion_tokens"], max(r["wall_s"], 1e-6)
                        tier = f" {r.get('tier', ''):<6}" if split_on else ""
                        # ttft only exists on streamed calls and IS the prefill; streamed calls report 0
                        # prompt tokens so read the pre figure, not tok/s
                        ttft = r.get("ttft_s") or 0.0
                        rate = r.get("gen_tok_s") or (gen / wall)
                        extra = f" {ttft:5.2f}s pre" if ttft else " " * 10
                        print(f"{DIM}      {r.get('name') or _call_name(r.get('system_head', '')):<14}"
                              f"{tier} {r['wall_s']:6.2f}s  {r['prompt_tokens']:>5} prompt"
                              f" {gen:>4} gen {extra}  {rate:5.1f} tok/s{RESET}")
                else:
                    print(f"{DIM}  [{elapsed:.1f}s]{RESET}")
            print()
    except KeyboardInterrupt:
        pass
    finally:
        reedus.close()
        if turns:
            print(f"\n{turns} turns, {total_s/turns:.1f}s/turn average.")
        print("later.")


if __name__ == "__main__":
    main()
