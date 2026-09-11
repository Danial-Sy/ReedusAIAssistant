#!/usr/bin/env python3
"""download and shape the augmentation data openWakeWord training needs."""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import urllib.request
from pathlib import Path

FEATURES_REPO = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main"
# 2000 hours of precomputed negative features, 17.3 GB. how the model learns what ISN'T the wake word.
ACAV_FEATURES = "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VAL_FEATURES = "validation_set_features.npy"

AUDIOSET_SHARD = ("https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/"
                  "bal_train09.tar")


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


def download(url: str, dest: Path) -> None:
    """Resumable download with a progress line, a 17 GB fetch will get interrupted."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have {dest.name} ({_human(dest.stat().st_size)})")
        return
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
        print(f"  resuming {dest.name} from {_human(have)}")
    else:
        print(f"  downloading {dest.name}")
    try:
        with urllib.request.urlopen(req) as resp, open(part, "ab") as fh:
            total = int(resp.headers.get("Content-Length", 0)) + have
            done = have
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if total:
                    pct = done / total * 100
                    print(f"\r    {_human(done)} / {_human(total)}  {pct:5.1f}%",
                          end="", flush=True)
        print()
    except KeyboardInterrupt:
        print(f"\n  interrupted, re-run to resume from {_human(part.stat().st_size)}")
        raise
    part.rename(dest)


def prep_rirs(root: Path) -> None:
    """MIT environmental impulse responses -> 16 kHz wavs."""
    out = root / "mit_rirs"
    if out.is_dir() and len(list(out.glob("*.wav"))) > 100:
        print(f"  have {len(list(out.glob('*.wav')))} RIRs")
        return
    out.mkdir(parents=True, exist_ok=True)
    import numpy as np
    import scipy.io.wavfile
    from datasets import load_dataset

    print("  streaming MIT RIRs …")
    ds = load_dataset("davidscripka/MIT_environmental_impulse_responses",
                      split="train", streaming=True)
    n = 0
    for row in ds:
        name = row["audio"]["path"].split("/")[-1]
        scipy.io.wavfile.write(str(out / name), 16000,
                               (row["audio"]["array"] * 32767).astype(np.int16))
        n += 1
        if n % 100 == 0:
            print(f"\r    {n}", end="", flush=True)
    print(f"\r    {n} RIRs")


def prep_background(root: Path, hours_music: float) -> None:
    """AudioSet noise + FMA music -> 16 kHz wavs the augmenter mixes under the speech."""
    out = root / "background_clips"
    have = len(list(out.glob("*.wav"))) if out.is_dir() else 0
    if have > 500:
        print(f"  have {have} background clips")
        return
    out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import scipy.io.wavfile

    # audioset: one balanced shard is plenty of ambience
    tar_path = root / "audioset" / "bal_train09.tar"
    download(AUDIOSET_SHARD, tar_path)
    extract_dir = root / "audioset" / "extracted"
    if not extract_dir.is_dir():
        extract_dir.mkdir(parents=True, exist_ok=True)
        print("  extracting AudioSet shard …")
        with tarfile.open(tar_path) as tf:
            tf.extractall(extract_dir)

    try:
        import torchaudio
        import torch
    except Exception as e:                                       # noqa: BLE001
        print(f"  ERROR: torchaudio needed to convert AudioSet ({e})", file=sys.stderr)
        raise

    flacs = list(extract_dir.rglob("*.flac"))
    print(f"  converting {len(flacs)} AudioSet clips to 16 kHz …")
    for i, f in enumerate(flacs):
        dest = out / (f.stem + ".wav")
        if dest.exists():
            continue
        try:
            wav, sr = torchaudio.load(str(f))
            wav = torchaudio.functional.resample(wav, sr, 16000)[0]
            scipy.io.wavfile.write(str(dest), 16000,
                                   (wav.numpy() * 32767).astype(np.int16))
        except Exception:                                        # noqa: BLE001
            continue
        if i % 200 == 0:
            print(f"\r    {i}/{len(flacs)}", end="", flush=True)
    print(f"\r    {len(list(out.glob('*.wav')))} clips from AudioSet")

    # fma: music, streamed so we never hold the whole set
    if hours_music <= 0:
        return
    from datasets import load_dataset
    print(f"  streaming ~{hours_music:g}h of FMA music …")
    ds = load_dataset("rudraml/fma", name="small", split="train", streaming=True)
    want = int(hours_music * 3600 / 30)          # 30s pieces
    n = 0
    for row in ds:
        if n >= want:
            break
        try:
            a = row["audio"]["array"]
            sr = row["audio"]["sampling_rate"]
            if sr != 16000:
                import torch
                import torchaudio
                a = torchaudio.functional.resample(
                    torch.from_numpy(np.asarray(a, dtype=np.float32)), sr, 16000).numpy()
            for j in range(0, max(0, len(a) - 16000 * 30), 16000 * 30):
                scipy.io.wavfile.write(str(out / f"fma_{n:05d}.wav"), 16000,
                                       (a[j:j + 16000 * 30] * 32767).astype(np.int16))
                n += 1
                if n >= want:
                    break
        except Exception:                                        # noqa: BLE001
            continue
        if n % 20 == 0:
            print(f"\r    {n}/{want}", end="", flush=True)
    print(f"\r    {len(list(out.glob('*.wav')))} background clips total")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="working directory for training data")
    ap.add_argument("--hours-music", type=float, default=1.0)
    ap.add_argument("--skip-features", action="store_true",
                    help="skip the 17.3 GB negative-feature download")
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"workdir: {root}")

    if not args.skip_features:
        print("\n[1/3] precomputed negative features (17.3 GB, the long one)")
        download(f"{FEATURES_REPO}/{ACAV_FEATURES}", root / ACAV_FEATURES)
        download(f"{FEATURES_REPO}/{VAL_FEATURES}", root / VAL_FEATURES)

    print("\n[2/3] room impulse responses")
    prep_rirs(root)

    print("\n[3/3] background noise + music")
    prep_background(root, args.hours_music)

    print("\ndone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
