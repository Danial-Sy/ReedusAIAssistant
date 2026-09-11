#!/usr/bin/env python3
"""DPO on the base model's own outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from reedus_companion import config                                   # noqa: E402

DEFAULT_BETA = 0.1
DEFAULT_LR = 5e-6
DEFAULT_EPOCHS = 1

OUT_DIR = config.PROJECT_ROOT / "artifacts" / "reedus-dpo"


def load_pairs(path: Path):
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def check_mix(rows) -> None:
    """Refuse a mix that would train hedging, the guard the earlier runs didn't have."""
    from collections import Counter
    kinds = Counter(r.get("kind") for r in rows)
    ab = kinds["A>B"] + kinds["A>P"]
    ac = kinds["A>C"]
    print(f"  pairs: {len(rows)}   {dict(kinds)}")
    print(f"  don't-claim-falsely (A>B + A>P): {ab}   don't-retreat (A>C): {ac}"
          f"   relevance (A>O): {kinds['A>O']}  [not in the ratio]")
    if not rows:
        raise SystemExit("  no pairs, run dpo_label first.")
    if ac == 0:
        raise SystemExit(
            "\n  REFUSING TO TRAIN: no A>C pairs.\n"
            "  Every pair says 'don't invent' and none says 'don't retreat'. The cheapest way\n"
            "  to satisfy that is to stop making claims, which is exactly how v2 went dry.\n"
            "  Sample more hedged responses (dpo_sample adds one per prompt) and re-label.\n")
    skew = ab / max(ac, 1)
    if skew > 1.6:
        raise SystemExit(
            f"\n  REFUSING TO TRAIN: A>B/A>C ratio is {skew:.2f} (want ~1.0, hard cap 1.6).\n"
            f"  Too heavy on 'don't invent' relative to 'don't retreat', the mix that\n"
            f"  produces a hedging model. Add hedged samples or trim A>B pairs.\n")
    if skew < 0.5:
        print(f"  WARNING: ratio {skew:.2f} leans toward A>C, trains confidence faster than\n"
              f"           accuracy. Usable, but watch the voice criterion.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", default="dpo_pairs.jsonl")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    ap.add_argument("--lr", type=float, default=DEFAULT_LR)
    ap.add_argument("--epochs", type=float, default=DEFAULT_EPOCHS)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the dataset and stop, no model load, no GPU needed")
    a = ap.parse_args()

    rows = load_pairs(Path(a.pairs))
    check_mix(rows)
    if a.dry_run:
        ex = rows[0]
        print("\n  sample pair:")
        print(f"    kind    : {ex.get('kind')}")
        print(f"    prompt  : {ex['prompt'][:100]}")
        print(f"    chosen  : {ex['chosen'][:120]}")
        print(f"    rejected: {ex['rejected'][:120]}")
        print("\n  dataset OK. Drop --dry-run to train.")
        return 0

    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
    from trl import DPOConfig, DPOTrainer

    tok = AutoTokenizer.from_pretrained(config.BASE_MODEL, use_fast=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_use_double_quant=True,
                             bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16)
    model = AutoModelForCausalLM.from_pretrained(
        config.BASE_MODEL, quantization_config=bnb, dtype=torch.bfloat16, device_map="auto")

    ds = Dataset.from_list([{"prompt": r["prompt"], "chosen": r["chosen"],
                             "rejected": r["rejected"]} for r in rows])
    split = ds.train_test_split(test_size=0.1, seed=0)

    args = DPOConfig(
        output_dir=a.out, beta=a.beta, learning_rate=a.lr, num_train_epochs=a.epochs,
        per_device_train_batch_size=1, gradient_accumulation_steps=8,
        logging_steps=10, eval_strategy="steps", eval_steps=50, save_steps=50,
        bf16=True, report_to="none", remove_unused_columns=False,
        max_length=config.N_CTX, max_prompt_length=config.N_CTX - 256,
    )
    trainer = DPOTrainer(
        model=model, args=args, processing_class=tok,
        train_dataset=split["train"], eval_dataset=split["test"],
        peft_config=LoraConfig(**config.LORA),
    )
    trainer.train()
    trainer.save_model(a.out)
    tok.save_pretrained(a.out)
    print(f"\n  adapter -> {a.out}")
    print("  evaluate BEFORE merging, specificity is the criterion that")
    print("  caught nothing in v2 and v3 because nobody measured it:")
    print("     python -m reedus_companion.train.dpo_eval --adapter", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
