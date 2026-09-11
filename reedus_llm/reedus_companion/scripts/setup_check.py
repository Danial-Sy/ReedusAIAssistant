"""environment sanity check: what's installed and what each role still needs."""
from __future__ import annotations
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from reedus_companion import config

GROUPS = {
    "train (GPU rig)": ["torch", "transformers", "datasets", "peft", "bitsandbytes", "accelerate"],
    "serve-cpu (E5 box)": ["llama_cpp", "fastembed", "ddgs"],
    "serve-gpu (main rig test)": ["torch", "transformers", "peft"],
    "voice": ["faster_whisper", "kokoro", "librosa", "sounddevice", "numpy"],
    "web ui (optional)": ["gradio"],
}


def have(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def main():
    print("== Reedus setup check ==\n")
    for group, mods in GROUPS.items():
        print(f"[{group}]")
        for m in mods:
            print(f"   {'OK ' if have(m) else '-- '} {m}")
        print()

    print("== paths ==")
    print(f"   base model        : {config.BASE_MODEL}")
    print(f"   normalized data   : {config.NORMALIZED_DIR}  ({'found' if config.NORMALIZED_DIR.exists() else 'MISSING'})")
    print(f"   voice seeds jsonl : {config.VOICE_SEEDS_JSONL}  ({'found' if config.VOICE_SEEDS_JSONL.exists() else 'MISSING'})")
    print(f"   LoRA adapter      : {config.OUTPUT_DIR}  ({'present' if config.OUTPUT_DIR.exists() else 'not trained yet'})")
    print(f"   GGUF              : {config.GGUF_QUANT}  ({'present' if config.GGUF_QUANT.exists() else 'not built yet'})")
    print(f"   TTS voice         : Kokoro '{config.KOKORO_VOICE}'  (pitch +{config.VOICE_PITCH_SEMITONES}, robot {config.ROBOT_AMOUNT}; model auto-downloads)")

    try:
        import torch
        print(f"\n   torch CUDA        : {'yes -> ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no (CPU only)'}")
    except Exception:
        print("\n   torch             : not installed")


if __name__ == "__main__":
    main()
