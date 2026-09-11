"""merge the LoRA adapter into the base and save fp16 for gguf conversion. abandoned path."""
import os
import sys
from pathlib import Path
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config

os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel


def main():
    print(f"Loading base {config.BASE_MODEL} (fp16)…")
    base = AutoModelForCausalLM.from_pretrained(config.BASE_MODEL, torch_dtype=torch.float16,
                                                device_map="cpu")
    print(f"Attaching adapter {config.OUTPUT_DIR}…")
    model = PeftModel.from_pretrained(base, str(config.OUTPUT_DIR))
    print("Merging…")
    model = model.merge_and_unload()
    config.MERGED_DIR.mkdir(parents=True, exist_ok=True)
    for stale in list(config.MERGED_DIR.glob("model*.safetensors")) + \
            list(config.MERGED_DIR.glob("model.safetensors.index.json")) + \
            list(config.MERGED_DIR.glob(".merge-complete")):
        stale.unlink()
        print(f"  removed stale {stale.name}")
    model.save_pretrained(str(config.MERGED_DIR), safe_serialization=True)
    AutoTokenizer.from_pretrained(str(config.OUTPUT_DIR)).save_pretrained(str(config.MERGED_DIR))
    print(f"Merged -> {config.MERGED_DIR}")


if __name__ == "__main__":
    main()
