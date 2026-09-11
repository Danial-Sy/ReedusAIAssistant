"""gpu backend (transformers). for testing on the 3090 box before quantizing for the xeon."""
from __future__ import annotations
import os
from typing import Callable, List, Dict, Optional

from reedus_companion import config

os.environ.setdefault("TRANSFORMERS_NO_TORCHVISION", "1")


class HFBackend:
    def __init__(self, adapter_dir: Optional[str] = None, merged_dir: Optional[str] = None,
                 load_4bit: bool = True, base_only: bool = False):
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

        if base_only:
            model_src = config.BASE_MODEL
            self.tok = AutoTokenizer.from_pretrained(config.BASE_MODEL, use_fast=True)
        elif merged_dir:
            model_src = merged_dir
            self.tok = AutoTokenizer.from_pretrained(merged_dir, use_fast=True)
        else:
            model_src = config.BASE_MODEL
            tok_src = adapter_dir or str(config.OUTPUT_DIR)
            self.tok = AutoTokenizer.from_pretrained(tok_src, use_fast=True)

        kwargs = dict(device_map="auto", attn_implementation="sdpa", torch_dtype=torch.bfloat16)
        if load_4bit and not merged_dir:
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)

        self.model = AutoModelForCausalLM.from_pretrained(model_src, **kwargs)

        if not base_only and (adapter_dir or (not merged_dir)):
            from peft import PeftModel
            adir = adapter_dir or str(config.OUTPUT_DIR)
            self.model = PeftModel.from_pretrained(self.model, adir)

        self.model.eval()
        if self.tok.pad_token_id is None:
            self.tok.pad_token = self.tok.eos_token
        self._torch = torch

    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "",
             cancel=None) -> str:
        import contextlib
        torch = self._torch
        prompt = self.tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.tok(prompt, return_tensors="pt", add_special_tokens=False).to(self.model.device)
        temp = config.TEMPERATURE if temperature is None else temperature
        do_sample = temp > 0
        gen_kwargs = dict(max_new_tokens=max_tokens, do_sample=do_sample,
                          repetition_penalty=config.REPEAT_PENALTY, pad_token_id=self.tok.pad_token_id)
        if do_sample:
            gen_kwargs["temperature"] = temp
            gen_kwargs["top_p"] = config.TOP_P
        ctx = (self.model.disable_adapter()
               if (not use_adapter and hasattr(self.model, "disable_adapter"))
               else contextlib.nullcontext())
        if on_token is not None:
            from threading import Thread
            from transformers import TextIteratorStreamer
            streamer = TextIteratorStreamer(self.tok, skip_prompt=True, skip_special_tokens=True)
            gen_kwargs["streamer"] = streamer

            def _run():
                with torch.no_grad(), ctx:
                    self.model.generate(**inputs, **gen_kwargs)

            t = Thread(target=_run, daemon=True)
            t.start()
            pieces = []
            for piece in streamer:
                if piece:
                    pieces.append(piece)
                    try:
                        on_token(piece)
                    except Exception:
                        pass
            t.join()
            return "".join(pieces).strip()

        with torch.no_grad(), ctx:
            out = self.model.generate(**inputs, **gen_kwargs)
        gen = out[0][inputs["input_ids"].shape[1]:]
        return self.tok.decode(gen, skip_special_tokens=True).strip()
