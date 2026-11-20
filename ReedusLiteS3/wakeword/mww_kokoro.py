#!/usr/bin/env python3
"""a second TTS engine (Kokoro) so the model hears more than piper's voices.

kokoro is deterministic per voice, so the variety comes from blending two voices' style
vectors at a random weight, plus speed and the vowel variants. positives are given as
phonemes (its G2P has never heard of Reedus); sound-alikes as plain text.

HOLDOUT voices are never used for training: `--holdout` renders only them, for mww_eval.py.

    python mww_kokoro.py --phonemes "ɹˈidəs" "ɹˈidɪs" --n 8000 --out samples/positive/kokoro
    python mww_kokoro.py --texts readers radius --n 300 --out samples/soundalike/kokoro
"""
from __future__ import annotations

import argparse
import random
import re
from pathlib import Path

import numpy as np

HOLDOUT = ("af_nicole", "am_fenrir", "bf_emma", "bm_george")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phonemes", nargs="*", default=[], help="American phonemes (misaki)")
    ap.add_argument("--texts", nargs="*", default=[])
    ap.add_argument("--n", type=int, required=True, help="clips per phoneme set / per text")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--holdout", action="store_true", help="only the held-out voices, no blends")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    import soundfile as sf
    import torch
    from huggingface_hub import list_repo_files
    from kokoro import KPipeline
    from scipy.signal import resample_poly

    rng = random.Random(a.seed)
    repo = "hexgrad/Kokoro-82M"
    voices = sorted(f[7:-3] for f in list_repo_files(repo)
                    if f.startswith("voices/") and f[7:9] in ("af", "am", "bf", "bm"))
    pool = [v for v in voices if (v in HOLDOUT) == a.holdout]
    us = KPipeline(lang_code="a", repo_id=repo)
    gb = KPipeline(lang_code="b", repo_id=repo, model=us.model)
    packs = {v: us.load_voice(v).float().cpu() for v in pool}
    print(f"{len(pool)} voices: {' '.join(pool)}")

    def british(ph: str) -> str:
        return ph.replace("ˈi", "ˈiː")

    def render(tokens: str, pipe, pack, speed: float) -> np.ndarray | None:
        for r in pipe.generate_from_tokens(tokens=tokens, voice=pack, speed=speed):
            if r.audio is not None:
                return r.audio.cpu().numpy()
        return None

    jobs = [("ph", p) for p in a.phonemes] + [("text", t) for t in a.texts]
    for kind, item in jobs:
        sub = a.out / re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_") if kind == "text" \
            else a.out / f"ph{a.phonemes.index(item)}"
        sub.mkdir(parents=True, exist_ok=True)
        have = len(list(sub.glob("*.wav")))
        for i in range(have, a.n):
            v1 = rng.choice(pool)
            accent_gb = v1.startswith("b")
            if a.holdout:
                pack, label = packs[v1], v1
            else:
                v2 = rng.choice(pool)
                w = rng.uniform(0.2, 0.8)
                pack, label = w * packs[v1] + (1 - w) * packs[v2], f"{v1}+{v2}"
            pipe = gb if accent_gb else us
            if kind == "ph":
                tokens = british(item) if accent_gb else item
                tokens += rng.choice(["", "", ".", "!", "?"])
            else:
                tokens, _ = pipe.g2p(item)
            speed = rng.uniform(0.8, 1.25)
            audio = render(tokens, pipe, pack, speed)
            if audio is None or len(audio) < 2400:
                print(f"  skipped {item} with {label}")
                continue
            pcm = resample_poly(audio, 2, 3)  # 24 kHz -> 16 kHz
            pcm = np.clip(pcm / max(1e-3, np.abs(pcm).max()) * 0.9 * 32767, -32768, 32767)
            sf.write(sub / f"{i}.wav", pcm.astype(np.int16), 16000, subtype="PCM_16")
        print(f"  {item}: {len(list(sub.glob('*.wav')))} clips -> {sub}")
    del torch


if __name__ == "__main__":
    main()
