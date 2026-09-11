# step 4: QLoRA on mistral-7b (legacy pipeline, superseded by reedus_companion/train)
import os, torch
from pathlib import Path
from typing import List, Dict, Any

from datasets import load_from_disk
from transformers import (
    AutoTokenizer,
    AutoModelForCausalLM,
    BitsAndBytesConfig,
    TrainingArguments,
)
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import Trainer
from transformers import EarlyStoppingCallback

# keep torchvision out of the import path
os.environ["TRANSFORMERS_NO_TORCHVISION"] = "1"

BASE_MODEL_PATH = str(Path.home() / "mistral_models" / "Mistral-7B-v0.3")
TRAIN_PATH = "data/blended/custom_train"
VAL_PATH   = "data/blended/custom_val"
MAX_LEN    = 2048
RESPONSE_TEMPLATE = "[/INST]"  # Mistral/LLaMA chat template boundary

print("Using model path:", BASE_MODEL_PATH)

# --- tokenizer ---
tok = AutoTokenizer.from_pretrained(BASE_MODEL_PATH, use_fast=True)
if tok.pad_token_id is None:
    tok.pad_token = tok.eos_token
tok.padding_side = "right"

if not getattr(tok, "chat_template", None):
    tok.chat_template = """{% set sys_msg = '' %}
{% if messages and messages[0]['role'] == 'system' %}
{% set sys_msg = messages[0]['content'] %}
{% set messages = messages[1:] %}
{% endif -%}
{{ bos_token }}
{%- for message in messages -%}
{%- if message['role'] == 'user' -%}
[INST] {%- if sys_msg %}<<SYS>>
{{ sys_msg }}
<</SYS>>

{%- set sys_msg = '' %}{% endif -%}{{ message['content'] }} [/INST]
{%- elif message['role'] == 'assistant' -%}
 {{ message['content'] }}{{ eos_token }}
{%- endif -%}
{%- endfor -%}"""


# --- 4-bit base ---
bnb_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
)
base = AutoModelForCausalLM.from_pretrained(
    BASE_MODEL_PATH,
    quantization_config=bnb_config,
    device_map="auto",
    attn_implementation="sdpa",
)

base = prepare_model_for_kbit_training(base)

# --- LoRA wrap ---
lora = LoraConfig(
    r=8, lora_alpha=32, lora_dropout=0.05,
    bias="none", task_type="CAUSAL_LM",
    target_modules=["q_proj","k_proj","v_proj","o_proj"],
)
model = get_peft_model(base, lora)
model.print_trainable_parameters()
model.gradient_checkpointing_enable()
model.config.use_cache = False

# --- datasets ---
train_ds = load_from_disk(TRAIN_PATH)
val_ds   = load_from_disk(VAL_PATH)

def to_text(ex):
    # serialize full chat; assistant is next to speak
    return {
        "text": tok.apply_chat_template(
            ex["messages"],
            tokenize=False,
            add_generation_prompt=True,
        )
    }

print(">> pre-map lens:", TRAIN_PATH, "=", len(load_from_disk(TRAIN_PATH)))
print(">> pre-map lens:", VAL_PATH,   "=", len(load_from_disk(VAL_PATH)))
# 1) make "text"
train_ds = train_ds.map(to_text, remove_columns=train_ds.column_names, num_proc=os.cpu_count() or 1)
val_ds   = val_ds.map(to_text,   remove_columns=val_ds.column_names,   num_proc=os.cpu_count() or 1)

# 2) tokenize
def tokenize_fn(batch):
    return tok(
        batch["text"],
        padding=False,              # pad in collator
        truncation=True,
        max_length=MAX_LEN,
        return_attention_mask=True,
    )

train_ds = train_ds.map(tokenize_fn, batched=True, remove_columns=["text"], num_proc=os.cpu_count() or 1)
val_ds   = val_ds.map(tokenize_fn,   batched=True, remove_columns=["text"], num_proc=os.cpu_count() or 1)

print(">> train rows:", len(train_ds), "| val rows:", len(val_ds))

# --- masking collator ---
class MaskAssistantOnlyCollator:
    """Labels are -100 for everything up to and including the last RESPONSE_TEMPLATE, so loss is on the reply only."""
    def __init__(self, tokenizer, response_template="[/INST]", max_length=2048):
        self.tok = tokenizer
        self.sep_ids = tokenizer(response_template, add_special_tokens=False).input_ids
        self.max_length = max_length

    @staticmethod
    def _find_last_subseq(seq: List[int], subseq: List[int]):
        if not subseq or len(subseq) > len(seq):
            return None
        last = None
        # naive scan, fast enough at ~2k tokens
        for i in range(0, len(seq) - len(subseq) + 1):
            if seq[i:i+len(subseq)] == subseq:
                last = i
        return last

    def __call__(self, features: List[Dict[str, Any]]) -> Dict[str, torch.Tensor]:
        # pad to longest in batch
        batch = self.tok.pad(
            features,
            padding=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        input_ids = batch["input_ids"]
        attn = batch["attention_mask"]
        labels = input_ids.clone()

        sep = self.sep_ids
        for i in range(input_ids.size(0)):
            ids = input_ids[i].tolist()
            # mask padding
            labels[i, attn[i] == 0] = -100
            # mask everything before (and including) the last '[/INST]'
            j = self._find_last_subseq(ids, sep)
            if j is not None:
                cut = j + len(sep)
                labels[i, :cut] = -100

        batch["labels"] = labels
        return batch

collator = MaskAssistantOnlyCollator(tok, RESPONSE_TEMPLATE, MAX_LEN)

# --- training args ---
import inspect

common_kwargs = dict(
    output_dir="outputs/mistral7b-reedus",
    max_steps=34000,
    save_steps=1000,
    eval_steps=1000,
    logging_steps=20,
    save_strategy="steps",
    learning_rate=1e-4,
    lr_scheduler_type="cosine",
    warmup_ratio=0.03,
    per_device_train_batch_size=1,
    gradient_accumulation_steps=16,
    num_train_epochs=7,
    bf16=True,
    gradient_checkpointing=True,
    optim="paged_adamw_8bit",
    max_grad_norm=0.3,
    weight_decay=0.0,
    logging_dir="outputs/logs",
    save_total_limit=5,
    report_to="none",
    remove_unused_columns=False,
    load_best_model_at_end=True,
    metric_for_best_model="eval_loss",
    greater_is_better=False,
)

params = inspect.signature(TrainingArguments.__init__).parameters
if "evaluation_strategy" in params:
    common_kwargs["evaluation_strategy"] = "steps"
    common_kwargs["eval_steps"] = 1000
elif "eval_strategy" in params:
    common_kwargs["eval_strategy"] = "steps"
    common_kwargs["eval_steps"] = 1000
else:
    common_kwargs["do_eval"] = True
    common_kwargs["eval_steps"] = 1000
common_kwargs["save_steps"] = common_kwargs["eval_steps"]
args = TrainingArguments(**common_kwargs)

print("Starting training…")
trainer = Trainer(
    model=model,
    args=args,
    train_dataset=train_ds,
    eval_dataset=val_ds,
    data_collator=collator,
    tokenizer=tok, 
)

trainer.train(resume_from_checkpoint="outputs/mistral7b-reedus/checkpoint-33000")
trainer.save_model("outputs/mistral7b-reedus-lora")
print("Done. LoRA adapter in outputs/mistral7b-reedus-lora")
