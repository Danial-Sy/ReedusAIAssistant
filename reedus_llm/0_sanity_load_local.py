from pathlib import Path
from transformers import AutoTokenizer, AutoModelForCausalLM

p = Path.home() / "mistral_models" / "Mistral-7B-v0.3"
print("Loading from:", p)

tok = AutoTokenizer.from_pretrained(str(p), use_fast=True)
print("Tokenizer OK:", type(tok))

m = AutoModelForCausalLM.from_pretrained(str(p), device_map="cpu")
print("Model OK:", type(m))
