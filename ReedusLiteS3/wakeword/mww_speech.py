#!/usr/bin/env python3
"""clean read speech (LibriSpeech train-clean-100) as negative features.

the shipped speech negatives are far-field (VOiCES), and the first model false-woke on close,
clean speech: "greatest", "bade us", "the reassured". half the utterances go in as they are,
half get a room (RIR) and background noise. features are cut into 5 s pieces like the shipped
negatives. utterances that actually say the name ("read us", "Rita's") are left out.

train-clean-100 shares no speakers with test-clean, which mww_eval.py uses for false wakes/hour.

    python mww_speech.py --parquet librispeech_train --out features/librispeech --work ~/reedus_mww
"""
from __future__ import annotations

import argparse
import io
import multiprocessing as mp
import re
from pathlib import Path

HOMOPHONES = re.compile(r"\b(READ US|REED US|REEDUS|RITA'?S|REID US)\b")
CHUNK = 500   # 5 s of 10 ms frames, same as kahrendt's negative sets


def worker(args: tuple) -> int:
    pq_path, out, work, seed = args
    dst = out / "training" / f"libri_{pq_path.stem}_mmap"
    if (dst / "done").exists():
        return 0
    import random

    import numpy as np
    import pyarrow.parquet as pq
    import soundfile as sf
    from mmap_ninja.ragged import RaggedMmap
    from microwakeword.audio.audio_utils import generate_features_for_clip
    from microwakeword.audio.augmentation import Augmentation

    rng = random.Random(seed + int(pq_path.stem))
    np.random.seed(seed + int(pq_path.stem))
    bg = [str(work / d) for d in ("fma_16k", "audioset_16k") if (work / d).is_dir()]
    aug = Augmentation(
        augmentation_duration_s=None,
        augmentation_probabilities={"AddBackgroundNoise": 0.6, "AddColorNoise": 0.2,
                                    "RIR": 0.6, "Gain": 1.0, "SevenBandParametricEQ": 0.2},
        impulse_paths=[str(work / "mit_rirs")], background_paths=bg,
        background_min_snr_db=5, background_max_snr_db=25, min_gain_db=-20, max_gain_db=0)

    table = pq.read_table(pq_path, columns=["audio", "text"])
    texts = table.column("text").to_pylist()
    audios = table.column("audio")

    def chunks():
        for i, text in enumerate(texts):
            if HOMOPHONES.search(text.upper()):
                continue
            pcm, sr = sf.read(io.BytesIO(audios[i].as_py()["bytes"]), dtype="float32")
            assert sr == 16000
            if rng.random() < 0.5:
                pcm = aug.augment_clip(pcm)
            spec = generate_features_for_clip(pcm, 10)
            for s in range(20, len(spec) - CHUNK // 2, CHUNK):
                yield spec[s:s + CHUNK]

    RaggedMmap.from_generator(out_dir=str(dst), sample_generator=chunks(),
                              batch_size=200, verbose=False)
    (dst / "done").touch()
    print(f"  {pq_path.name} done", flush=True)
    return 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--procs", type=int, default=14)
    ap.add_argument("--seed", type=int, default=99)
    a = ap.parse_args()
    (a.out / "training").mkdir(parents=True, exist_ok=True)
    files = sorted(a.parquet.glob("*.parquet"))
    with mp.get_context("spawn").Pool(min(a.procs, len(files))) as pool:
        pool.map(worker, [(f, a.out, a.work, a.seed) for f in files])
    print(f"speech negatives -> {a.out}")


if __name__ == "__main__":
    main()
