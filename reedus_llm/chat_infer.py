import os, torch, math
from pathlib import Path
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig
from peft import PeftModel

os.environ["TRANSFORMERS_NO_TORCHVISION"] = "1"

BASE = str(Path.home() / "mistral_models" / "Mistral-7B-v0.3")
ADPT = "outputs/mistral7b-reedus-lora"

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
)

tok = AutoTokenizer.from_pretrained(BASE, use_fast=True)
if tok.pad_token_id is None:
    tok.pad_token = tok.eos_token
tok.padding_side = "right"

# add a simple chat template if base doesn't ship one
if not getattr(tok, "chat_template", None):
    tok.chat_template = """{% set sys_msg='' %}{% if messages and messages[0]['role']=='system' %}{% set sys_msg=messages[0]['content'] %}{% set messages=messages[1:] %}{% endif %}{{ bos_token }}{% for m in messages %}{% if m['role']=='user' %}[INST] {% if sys_msg %}<<SYS>>
{{ sys_msg }}
<</SYS>>

{% set sys_msg='' %}{% endif %}{{ m['content'] }} [/INST]{% elif m['role']=='assistant' %} {{ m['content'] }}{{ eos_token }}{% endif %}{% endfor %}"""

base = AutoModelForCausalLM.from_pretrained(
    BASE, quantization_config=bnb, device_map="auto", attn_implementation="sdpa"
)
model = PeftModel.from_pretrained(base, ADPT)
model.eval()

def chat(prompt, system=None, max_new_tokens=256, temperature=0.7, top_p=0.9):
    msgs = []
    if system: msgs.append({"role":"system","content":system})
    msgs.append({"role":"user","content":prompt})
    text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok([text], return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=True,
            temperature=temperature,
            top_p=top_p,
            repetition_penalty=1.1,
            eos_token_id=tok.eos_token_id,
        )
    return tok.decode(out[0], skip_special_tokens=True).split("[/INST]")[-1].strip()

if __name__ == "__main__":
    print(chat("Hey Reedus, introduce yourself in two sentences.", system="You are Reedus, a friendly mobile robot assistant."))
