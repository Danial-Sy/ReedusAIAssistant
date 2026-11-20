#!/usr/bin/env python3
"""background audio for microWakeWord augmentation: AudioSet + FMA music -> 16 kHz mono wavs.

the MIT room impulse responses come from the openWakeWord workdir if it's there (same files),
otherwise they're streamed from Hugging Face. the negative *features* (speech, dinner party,
no speech) are separate, `train_mww.sh data` fetches those as zips.

the data has mixed licenses: a model trained on it is for personal, non-commercial use.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import urllib.request
from pathlib import Path

AUDIOSET = "https://huggingface.co/datasets/agkphysics/AudioSet/resolve/main/data/bal_train/{}"
FMA_XS = "https://huggingface.co/datasets/mchl914/fma_xsmall/resolve/main/fma_xs.zip"


def fetch(url: str, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  have {dest.name}")
        return
    print(f"  downloading {url}")
    subprocess.run(["wget", "-c", "-q", "--show-progress", "--progress=dot:giga",
                    "-O", str(dest), url], check=True)


def to_16k(files: list[Path], out: Path) -> None:
    """Decode anything ffmpeg reads to 16 kHz mono int16 wavs, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    out.mkdir(parents=True, exist_ok=True)

    def one(f: Path) -> None:
        dst = out / (f.stem + ".wav")
        if dst.exists():
            return
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(f),
                        "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", str(dst)],
                       check=False)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(one, files))
    print(f"  {out.name}: {len(list(out.glob('*.wav')))} clips")


def rirs(root: Path, oww_rirs: Path | None) -> None:
    out = root / "mit_rirs"
    if out.exists() and len(list(out.glob("*.wav"))) > 100:
        print(f"  have {len(list(out.glob('*.wav')))} RIRs")
        return
    if oww_rirs and oww_rirs.is_dir() and len(list(oww_rirs.glob("*.wav"))) > 100:
        if out.is_symlink() or out.exists():
            out.unlink() if out.is_symlink() else shutil.rmtree(out)
        out.symlink_to(oww_rirs)
        print(f"  linked RIRs from {oww_rirs}")
        return
    import numpy as np
    import scipy.io.wavfile
    from datasets import load_dataset
    out.mkdir(parents=True, exist_ok=True)
    print("  streaming MIT RIRs")
    for row in load_dataset("davidscripka/MIT_environmental_impulse_responses",
                            split="train", streaming=True):
        name = row["audio"]["path"].split("/")[-1]
        scipy.io.wavfile.write(out / name, 16000,
                               (row["audio"]["array"] * 32767).astype(np.int16))


def audioset(root: Path, shards: list[str]) -> None:
    """the HF mirror is parquet now (it was .tar of flacs): pull the audio bytes out, then decode."""
    import pyarrow.parquet as pq
    raw = root / "audioset"
    raw.mkdir(parents=True, exist_ok=True)
    for shard in shards:
        marker = raw / (shard + ".extracted")
        if marker.exists():
            continue
        pqf = raw / shard
        fetch(AUDIOSET.format(shard), pqf)
        table = pq.read_table(pqf, columns=["audio"])
        for i, a in enumerate(table.column("audio").to_pylist()):
            name = Path(a.get("path") or f"{pqf.stem}_{i}.flac").name
            (raw / name).write_bytes(a["bytes"])
        pqf.unlink()
        marker.touch()
    to_16k(sorted(f for f in raw.iterdir() if f.suffix in (".flac", ".wav", ".mp3", ".ogg")),
           root / "audioset_16k")


def fma(root: Path) -> None:
    raw = root / "fma"
    raw.mkdir(parents=True, exist_ok=True)
    marker = raw / ".extracted"
    if not marker.exists():
        z = raw / "fma_xs.zip"
        fetch(FMA_XS, z)
        subprocess.run(["unzip", "-q", "-o", str(z), "-d", str(raw)], check=True)
        z.unlink()
        marker.touch()
    to_16k(sorted(raw.glob("**/*.mp3")), root / "fma_16k")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--oww-rirs", type=Path, default=None,
                    help="reuse the openWakeWord workdir's mit_rirs/")
    ap.add_argument("--audioset-shards", nargs="+",
                    default=["00.parquet", "01.parquet", "02.parquet"])
    a = ap.parse_args()
    a.root.mkdir(parents=True, exist_ok=True)
    print("== room impulse responses ==")
    rirs(a.root, a.oww_rirs)
    print("== AudioSet ==")
    audioset(a.root, a.audioset_shards)
    print("== FMA music ==")
    fma(a.root)


if __name__ == "__main__":
    main()
