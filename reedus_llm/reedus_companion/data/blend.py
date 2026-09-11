"""blend the public dialogue sets with the hand-authored reedus data for training."""
import os
import re
import sys
import json
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config
from reedus_companion.serve.persona import REEDUS_SYSTEM
from reedus_companion.serve.tools import render_tools_block

from datasets import load_from_disk, concatenate_datasets, Dataset

SEED = 1234

TRAIN_SYSTEM = REEDUS_SYSTEM + "\n\n" + render_tools_block()
TARGET = 12_000

MIX = {
    "empathetic": 0.40, "daily": 0.30, "soda": 0.15, "custom": 0.10, "skill": 0.05,
}

# identity-poisoning filter: drop public rows where the assistant claims a name/identity
_AGE = re.compile(r"\b(how old are you|what'?s your age|i'?m \d+ years old|i am \d+ years old)\b",
                  re.IGNORECASE)
_NAME_PHRASE = re.compile(
    r"\b(my name is|my name's|call me|i'?m called|i am called|i go by|they call me|"
    r"name's|i'?m known as)\b", re.IGNORECASE)
_INTRO = re.compile(r"\b(?:I'?m|I am|This is)\s+([A-Z][a-z]{2,})")
_INTRO_OK = {"Reedus", "Sorry", "Glad", "Happy", "Good", "Great", "Sure", "Here", "Just",
             "Going", "Doing", "Trying", "Really", "Not", "Always", "Still", "Also", "Okay",
             "Fine", "Proud", "Curious", "Excited", "Afraid", "Worried", "Ready"}


def _claims_identity(text: str) -> bool:
    text = text or ""
    if _AGE.search(text) or _NAME_PHRASE.search(text):
        return True
    m = _INTRO.search(text)
    return bool(m and m.group(1) not in _INTRO_OK)


def _sample(ds, n, seed=SEED):
    if n <= 0:
        return ds.select([])
    if len(ds) <= n:
        return ds.shuffle(seed=seed)
    idx = list(range(len(ds)))
    random.Random(seed).shuffle(idx)
    return ds.select(idx[:n])


def _with_system(messages):
    msgs = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": TRAIN_SYSTEM}] + msgs


def _valid(messages):
    if not messages:
        return False
    roles = [m.get("role") for m in messages if m.get("role") in ("user", "assistant")]
    if not (roles and "user" in roles and roles[-1] == "assistant"):
        return False
    for m in messages:
        if m.get("role") == "assistant" and _claims_identity(m.get("content", "")):
            return False
    return True


def _load_jsonl(path):
    path = Path(path)
    convos = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            convos.append(json.loads(line)["messages"])
    return convos


def main():
    os.makedirs(config.BLEND_DIR, exist_ok=True)
    total = sum(MIX.values())
    mix = {k: v / total for k, v in MIX.items()}

    pieces = []
    print("Loading + sampling normalized sets:")
    for name, frac in mix.items():
        p = config.NORMALIZED_DIR / name
        if not p.exists():
            print(f"  ! skip {name}: not found at {p}")
            continue
        ds = load_from_disk(str(p))
        s = _sample(ds, int(TARGET * frac))
        pieces.append(s)
        print(f"  {name:11s} {len(s):>7d}  (of {len(ds)})")
    if not pieces:
        raise SystemExit("No normalized datasets found. Run the v1 fetch/normalize steps first.")

    blend = concatenate_datasets(pieces)
    blend = blend.filter(lambda ex: _valid(ex["messages"]), num_proc=os.cpu_count() or 1)
    blend = blend.map(lambda ex: {"messages": _with_system(ex["messages"])},
                      num_proc=os.cpu_count() or 1)

    def _oversample(convos, repeat):
        rows = []
        for _ in range(repeat):
            for conv in convos:
                rows.append({"messages": _with_system(conv)})
        return rows

    extra = []
    convo = _load_jsonl(config.CONVO_SEEDS_JSONL)
    if convo:
        short = [c for c in convo if len(c) < 6]
        if short:
            raise SystemExit(
                f"{config.CONVO_SEEDS_JSONL.name}: {len(short)} conversation(s) have fewer than 6 "
                f"messages. This file exists specifically to teach multi-turn behaviour, short "
                f"examples here recreate the v2 failure. Move them to reedus_voice.jsonl.")
        rows = _oversample(convo, config.CONVO_REPEAT)
        extra.append(Dataset.from_list(rows))
        turns = sum(len(c) for c in convo) / len(convo)
        print(f"  convo_seeds    {len(convo)} convos x{config.CONVO_REPEAT} = {len(rows)} rows "
              f"({turns:.1f} msgs/convo avg)")
    voice = _load_jsonl(config.VOICE_SEEDS_JSONL)
    if voice:
        rows = _oversample(voice, config.VOICE_SEED_REPEAT)
        extra.append(Dataset.from_list(rows))
        print(f"  voice_seeds    {len(voice)} convos x{config.VOICE_SEED_REPEAT} = {len(rows)} rows")
    identity = _load_jsonl(config.IDENTITY_SEEDS_JSONL)
    if identity:
        rows = _oversample(identity, config.IDENTITY_REPEAT)
        extra.append(Dataset.from_list(rows))
        print(f"  identity_seeds {len(identity)} convos x{config.IDENTITY_REPEAT} = {len(rows)} rows")
    tools = _load_jsonl(config.TOOLS_SEEDS_JSONL)
    if tools:
        rows = _oversample(tools, config.TOOLS_REPEAT)
        extra.append(Dataset.from_list(rows))
        print(f"  tools_seeds    {len(tools)} convos x{config.TOOLS_REPEAT} = {len(rows)} rows")
    if extra:
        blend = concatenate_datasets([blend] + extra)

    blend = blend.shuffle(seed=SEED)

    n = len(blend)
    val_n = max(2000, int(0.02 * n))
    train = blend.select(range(0, n - val_n))
    val = blend.select(range(n - val_n, n))
    train.save_to_disk(str(config.TRAIN_PATH))
    val.save_to_disk(str(config.VAL_PATH))

    print(f"\nTotal: {n} | train {len(train)} -> {config.TRAIN_PATH} | val {len(val)} -> {config.VAL_PATH}")
    print("Done.")


if __name__ == "__main__":
    main()
