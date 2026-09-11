"""QLoRA fine-tune of the base model on the 3090. abandoned path, the base model ships (see LLM_NOTES.md)."""
import os
import sys
from pathlib import Path
from typing import List, Dict, Any

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config

os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")


def _preflight() -> None:
    """Fail early with something actionable."""
    if not torch.cuda.is_available():
        raise SystemExit(
            "\n  CUDA is not available in this environment, so there is nothing to train on.\n"
            f"  torch {torch.__version__}, the '+cpu' suffix (or no '+cu') means CPU-only.\n\n"
            "  QLoRA needs a GPU. Fix by installing CUDA torch into a TRAINING env, kept\n"
            "  separate from the serving env (which wants CPU torch + CPU llama-cpp, mixing\n"
            "  them is what produces torch/torchvision mismatches):\n\n"
            "    conda create -n reedus-train python=3.11 -y && conda activate reedus-train\n"
            "    pip install torch --index-url https://download.pytorch.org/whl/cu121\n"
            "    pip install -r reedus_companion/requirements/train.txt\n"
            "    pip install bitsandbytes\n")

    major = torch.cuda.get_device_capability()[0]
    if major < 8:                      # bf16 needs ampere
        raise SystemExit(
            f"\n  {torch.cuda.get_device_name(0)} (compute {major}.x) has no bf16 support.\n"
            "  Re-run with fp16 instead:  REEDUS_TRAIN_PRECISION=fp16\n")

    print(f"  GPU: {torch.cuda.get_device_name(0)} "
          f"({torch.cuda.get_device_properties(0).total_memory/1e9:.0f} GB), "
          f"torch {torch.__version__}")


_preflight()

from datasets import load_from_disk
from transformers import (AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig,
                          TrainingArguments, Trainer, EarlyStoppingCallback)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

ASSISTANT_HEADER = "<|im_start|>assistant\n"
IM_END = "<|im_end|>"

tok = AutoTokenizer.from_pretrained(config.BASE_MODEL, use_fast=True)
if tok.pad_token_id is None:
    tok.pad_token = tok.eos_token
tok.padding_side = "right"

bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_use_double_quant=True,
                         bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
model = AutoModelForCausalLM.from_pretrained(
    config.BASE_MODEL, quantization_config=bnb, device_map="auto",
    attn_implementation="sdpa", torch_dtype=torch.bfloat16)
model = prepare_model_for_kbit_training(model)
model = get_peft_model(model, LoraConfig(**config.LORA))
model.print_trainable_parameters()
model.gradient_checkpointing_enable()
model.config.use_cache = False

train_ds = load_from_disk(str(config.TRAIN_PATH))
val_ds = load_from_disk(str(config.VAL_PATH))
print(f"train rows: {len(train_ds)} | val rows: {len(val_ds)}")


def render(ex):
    return {"text": tok.apply_chat_template(ex["messages"], tokenize=False,
                                            add_generation_prompt=False)}


def tokenize(batch):
    return tok(batch["text"], truncation=True, max_length=config.MAX_LEN, add_special_tokens=False)


for ds_name in ("train_ds", "val_ds"):
    ds = locals()[ds_name]
    ds = ds.map(render, remove_columns=ds.column_names, num_proc=os.cpu_count() or 1)
    ds = ds.map(tokenize, batched=True, remove_columns=["text"], num_proc=os.cpu_count() or 1)
    locals()[ds_name] = ds
train_ds, val_ds = locals()["train_ds"], locals()["val_ds"]


class AssistantOnlyCollator:
    """Labels are -100 except assistant response spans, multi-turn safe."""
    def __init__(self, tokenizer, max_length):
        self.tok = tokenizer
        self.max_length = max_length
        self.hdr = tokenizer(ASSISTANT_HEADER, add_special_tokens=False).input_ids
        self.end = tokenizer(IM_END, add_special_tokens=False).input_ids

    @staticmethod
    def _find_all(seq, sub):
        out, n, m = [], len(seq), len(sub)
        if m == 0 or m > n:
            return out
        i = 0
        while i <= n - m:
            if seq[i:i + m] == sub:
                out.append(i); i += m
            else:
                i += 1
        return out

    def __call__(self, features: List[Dict[str, Any]]):
        batch = self.tok.pad(features, padding=True, max_length=self.max_length, return_tensors="pt")
        input_ids, attn = batch["input_ids"], batch["attention_mask"]
        labels = torch.full_like(input_ids, -100)
        for i in range(input_ids.size(0)):
            ids = input_ids[i].tolist()
            for s in self._find_all(ids, self.hdr):
                rs = s + len(self.hdr)
                ends = self._find_all(ids[rs:], self.end)
                re_ = rs + ends[0] + len(self.end) if ends else len(ids)
                labels[i, rs:re_] = input_ids[i, rs:re_]
            labels[i, attn[i] == 0] = -100
        batch["labels"] = labels
        return batch


args = TrainingArguments(
    output_dir=str(config.OUTPUT_DIR), report_to="none", remove_unused_columns=False,
    load_best_model_at_end=True, metric_for_best_model="eval_loss", greater_is_better=False,
    eval_strategy="steps", save_strategy="steps", logging_dir=str(config.OUTPUT_DIR / "logs"),
    **config.TRAIN_ARGS,
)
trainer = Trainer(
    model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds,
    data_collator=AssistantOnlyCollator(tok, config.MAX_LEN), processing_class=tok,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=config.EARLY_STOPPING_PATIENCE)],
)

if __name__ == "__main__":
    resume = "--resume" in sys.argv
    print("Training Reedus…" + (" (resuming from last checkpoint)" if resume else ""))
    trainer.train(resume_from_checkpoint=resume)
    trainer.save_model(str(config.OUTPUT_DIR))
    tok.save_pretrained(str(config.OUTPUT_DIR))
    print(f"Done. Adapter -> {config.OUTPUT_DIR}")
