"""optional gradio text UI:  python -m reedus_companion.serve.app --backend gguf|hf"""
from __future__ import annotations
import argparse

from reedus_companion.serve.core import Reedus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "gguf"], default="gguf")
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--gguf", default=None)
    args = ap.parse_args()

    if args.backend == "hf":
        from reedus_companion.serve.backend_hf import HFBackend
        backend = HFBackend(adapter_dir=args.adapter, merged_dir=args.merged)
    else:
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        backend = LlamaCppBackend(gguf_path=args.gguf)

    reedus = Reedus(backend)
    import gradio as gr
    gr.ChatInterface(
        fn=lambda message, history: reedus.reply(message),
        title="Reedus",
        description="Your local companion. Remembers you, searches the web when he needs to.",
        theme="soft",
    ).launch(server_name="0.0.0.0", server_port=7860)


if __name__ == "__main__":
    main()
