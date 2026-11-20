#!/usr/bin/env python3
"""augment TTS clips and turn them into microWakeWord spectrogram features (RaggedMmap).

microWakeWord's own loop is one process; this shards the clips across processes, each writing
its own `<split>/<name>_<shard>_mmap`. the trainer picks up every folder ending in `mmap`.

    python mww_features.py --clips samples/positive --out features/positive --work ~/reedus_mww
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
from pathlib import Path

# split name in Clips -> folder name the trainer reads, how many augmented copies, slide frames
SPLITS = (("train", "training", None, 10), ("validation", "validation", 1, 10),
          ("test", "testing", 1, 1))


def worker(args: tuple) -> None:
    clips_dir, out, work, shard, nshards, repeat_train, seed = args
    import random

    import numpy as np
    from mmap_ninja.ragged import RaggedMmap
    from microwakeword.audio.augmentation import Augmentation
    from microwakeword.audio.clips import Clips
    from microwakeword.audio.spectrograms import SpectrogramGeneration

    # Clips' second split (test vs validation) is unseeded and draws from numpy's global state,
    # so every shard has to start from the same state or they'd disagree on which clip is which
    np.random.seed(seed)
    clips = Clips(input_directory=str(clips_dir), file_pattern="**/*.wav",
                  max_clip_duration_s=None, remove_silence=False,
                  random_split_seed=10, split_count=0.1)
    for k in list(clips.split_clips):
        clips.split_clips[k] = clips.split_clips[k].shard(nshards, shard, contiguous=False)
    np.random.seed(seed + 1000 + shard)   # different augmentations per shard
    random.seed(seed + 1000 + shard)

    bg = [str(work / d) for d in ("fma_16k", "audioset_16k") if (work / d).is_dir()]
    augmenter = Augmentation(
        augmentation_duration_s=3.2,
        augmentation_probabilities={
            "SevenBandParametricEQ": 0.1, "TanhDistortion": 0.1, "PitchShift": 0.1,
            "BandStopFilter": 0.1, "AddColorNoise": 0.1, "AddBackgroundNoise": 0.75,
            "Gain": 1.0, "RIR": 0.5,
        },
        impulse_paths=[str(work / "mit_rirs")], background_paths=bg,
        background_min_snr_db=-5, background_max_snr_db=10,
        min_jitter_s=0.195, max_jitter_s=0.205)

    for split, folder, repeat, slide in SPLITS:
        dst = out / folder / f"{out.name}_{shard:02d}_mmap"
        if (dst / "done").exists():
            continue
        spec = SpectrogramGeneration(clips=clips, augmenter=augmenter,
                                     slide_frames=slide, step_ms=10)
        RaggedMmap.from_generator(
            out_dir=str(dst),
            sample_generator=spec.spectrogram_generator(split=split,
                                                        repeat=repeat or repeat_train),
            batch_size=100, verbose=False)
        (dst / "done").touch()
        print(f"  [{out.name} {shard}] {folder} done", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True, help="has mit_rirs/, fma_16k/, ...")
    ap.add_argument("--shards", type=int, default=max(1, (os.cpu_count() or 4) - 4))
    ap.add_argument("--repeat", type=int, default=2, help="augmented copies per training clip")
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()
    for _, folder, _, _ in SPLITS:
        (a.out / folder).mkdir(parents=True, exist_ok=True)
    jobs = [(a.clips, a.out, a.work, i, a.shards, a.repeat, a.seed) for i in range(a.shards)]
    with mp.get_context("spawn").Pool(a.shards) as pool:
        pool.map(worker, jobs)
    print(f"features -> {a.out}")


if __name__ == "__main__":
    main()
