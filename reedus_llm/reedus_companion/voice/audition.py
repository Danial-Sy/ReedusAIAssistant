"""audition kokoro voices through the real pipeline so you can pick one by ear."""
from __future__ import annotations
import argparse
from pathlib import Path

from reedus_companion import config

VOICES = {
    "af": ["af_heart", "af_bella", "af_sarah", "af_sky", "af_nicole", "af_nova", "af_aoede", "af_kore"],
    "am": ["am_michael", "am_adam", "am_eric", "am_liam", "am_onyx", "am_fenrir", "am_puck", "am_echo"],
    "bf": ["bf_emma", "bf_isabella", "bf_alice", "bf_lily"],
    "bm": ["bm_george", "bm_daniel", "bm_lewis", "bm_fable"],   # british males, the JARVIS lane
}
GROUPS = {
    "female": VOICES["af"] + VOICES["bf"],
    "male": VOICES["am"] + VOICES["bm"],
    "american": VOICES["af"] + VOICES["am"],
    "british": VOICES["bf"] + VOICES["bm"],
    "all": VOICES["af"] + VOICES["am"] + VOICES["bf"] + VOICES["bm"],
}
QUICK = ["af_heart", "af_sarah", "af_bella", "am_michael", "am_onyx", "bm_george", "bf_emma"]

DEFAULT_LINE = ("Hey! It's Reedus. Wait... are you serious right now? *laughs* okay, "
                "honestly, that's kind of amazing.")


def _tag(pitch, robot):
    return f"p{int(round(pitch))}_r{int(round(robot * 100))}"


def _synth(voice, pitch, robot, line, out_dir):
    from reedus_companion.voice.tts import TTS
    tts = TTS(voice=voice, pitch=pitch, robot=robot)
    path = out_dir / f"{voice}__{_tag(pitch, robot)}.wav"
    tts.to_wav(line, str(path))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--line", default=DEFAULT_LINE)
    ap.add_argument("--group", choices=sorted(GROUPS), default=None,
                    help="which voice group to audition (default: a quick cross-section)")
    ap.add_argument("--voices", nargs="*", default=None, help="explicit voice list")
    ap.add_argument("--matrix", action="store_true",
                    help="for each voice, render {no-pitch, pitched} x {no-robot, robotized}")
    ap.add_argument("--pitch", type=float, default=config.VOICE_PITCH_SEMITONES,
                    help="the 'pitched' value for the matrix (semitones up)")
    ap.add_argument("--robot", type=float, default=config.ROBOT_AMOUNT,
                    help="the 'robotized' value for the matrix (0..1)")
    ap.add_argument("--blend", nargs="*", default=None,
                    help="audition ONE blend, e.g. --blend af_heart:0.7 af_sarah:0.3")
    args = ap.parse_args()

    out_dir = Path(config.VOICE_DIR) / "audition"
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.blend:
        blend = [(n, float(w) if w else 1.0) for n, _, w in (p.partition(":") for p in args.blend)]
        print(f"Auditioning blend {blend} -> {out_dir}")
        config.KOKORO_VOICE_BLEND = blend
        from reedus_companion.voice.tts import TTS
        tts = TTS()
        name = "blend_" + "_".join(f"{n}{int(w*100)}" for n, w in blend)
        path = out_dir / f"{name}.wav"
        tts.to_wav(args.line, str(path))
        print(f"  wrote {path.name}")
        print(f"\nLike it? config.py:  KOKORO_VOICE_BLEND = {blend}")
        return

    voices = args.voices or (GROUPS[args.group] if args.group else QUICK)

    if args.matrix:
        variants = [(0.0, 0.0), (0.0, args.robot), (args.pitch, 0.0), (args.pitch, args.robot)]
        print(f"Matrix: {len(voices)} voices x {len(variants)} variants -> {out_dir}\n"
              f"  pitched=+{args.pitch} semis, robot={args.robot}\n  line: {args.line!r}\n")
        for v in voices:
            for pitch, robot in variants:
                try:
                    path = _synth(v, pitch, robot, args.line, out_dir)
                    print(f"  OK  {path.name}")
                except Exception as e:
                    print(f"  --  {v} (p{pitch} r{robot}) failed: {e}")
        _final_hint()
        return

    print(f"Quick scan: {len(voices)} voices @ pitch +{config.VOICE_PITCH_SEMITONES}, "
          f"robot {config.ROBOT_AMOUNT} -> {out_dir}\n  line: {args.line!r}\n")
    for v in voices:
        try:
            path = _synth(v, config.VOICE_PITCH_SEMITONES, config.ROBOT_AMOUNT, args.line, out_dir)
            print(f"  OK  {path.name}")
        except Exception as e:
            print(f"  --  {v} failed: {e}")
    _final_hint()


def _final_hint():
    print(f"\nListen through {Path(config.VOICE_DIR) / 'audition'}, then set your pick in config.py:")
    print("  KOKORO_VOICE = \"am_michael\"            # single voice")
    print("  VOICE_PITCH_SEMITONES = 4               # 0 = natural, higher = cuter")
    print("  ROBOT_AMOUNT = 0.0                      # 0 = no robot, 0.15 = light, 0.4 = heavy")
    print("  KOKORO_VOICE_BLEND = [(\"af_heart\", 0.7), (\"af_sarah\", 0.3)]   # or a blend")


if __name__ == "__main__":
    main()
