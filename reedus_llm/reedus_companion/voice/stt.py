"""speech-to-text via faster-whisper."""
from __future__ import annotations
import os
import re
import sys
from typing import Optional

from reedus_companion import config

_NAME = "Reedus"
_ALWAYS = re.compile(
    r"(?i)\b(breeders?|breeters?|reedos|riedus|readus|redus|ritus|reedis|reed[\s-]?us)\b"
)
_VOCATIVE = re.compile(
    r"(^|[\s,])((?:hey|hi|yo|okay|ok|so|um|uh)[\s,]+)(readers?|radius|redis)\b"
    r"|^(readers?|radius|redis)\b",
    re.I,
)


def _shaped(raw: str) -> str:
    compact = raw.replace(" ", "").replace("-", "")
    if compact.isupper() and compact.isalpha():
        return _NAME.upper()
    return _NAME


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _collapse_repeats(text: str) -> str:
    parts = [p for p in _SENT_SPLIT.split((text or "").strip()) if p.strip()]
    if len(parts) < 2:
        return text
    out = []
    for part in parts:
        key = re.sub(r"[^a-z0-9]+", "", part.lower())
        prev = re.sub(r"[^a-z0-9]+", "", out[-1].lower()) if out else None
        if key and key == prev:
            continue
        out.append(part)
    return " ".join(out)


def _echoes_prompt(text: str, prompt: str) -> bool:
    """The transcript is just the initial_prompt handed back instead of real speech."""
    def norm(t):
        return re.sub(r"[^a-z0-9]+", "", (t or "").lower())
    t, pr = norm(text), norm(prompt)
    if not t or not pr or len(t) < 8:
        return False
    return t in pr


def canon_name(text: str) -> str:
    """Rewrite whisper's guesses for his name to Reedus."""
    if not (text or "").strip():
        return text or ""

    out = _ALWAYS.sub(lambda m: _shaped(m.group(0)), text)

    def _voc(m: re.Match) -> str:
        if m.lastindex and m.lastindex >= 3 and m.group(3):
            return f"{m.group(1)}{m.group(2)}{_shaped(m.group(3))}"
        return _shaped(m.group(0))

    out = _VOCATIVE.sub(_voc, out)
    out = re.sub(r"(?i)\b(na+u?rr?u?to+|nar+uto+|nauruto|norruto)\b", "Naruto", out)
    return out


class STT:
    def __init__(self, model: Optional[str] = None, device: Optional[str] = None,
                 compute: Optional[str] = None):
        from faster_whisper import WhisperModel
        self._model_name = model or config.STT_MODEL
        dev = device or config.STT_DEVICE
        if dev == "auto":
            try:
                import torch
                dev = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                dev = "cpu"
        compute = compute or (config.STT_COMPUTE if dev == "cpu" else "float16")
        self.model = WhisperModel(self._model_name, device=dev, compute_type=compute)
        self.device = dev
        self._checked = False

    def _fallback_to_cpu(self):
        from faster_whisper import WhisperModel
        print("[stt] CUDA runtime unavailable for CTranslate2, falling back to CPU (int8). "
              "See reedus_companion/voice/stt.py header to enable GPU whisper.")
        self.model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        self.device = "cpu"

    def transcribe(self, audio) -> str:
        """audio: path (str) or float32 mono numpy array at 16 kHz."""
        try:
            kw = {}
            prompt = getattr(config, "STT_INITIAL_PROMPT", "") or ""
            if prompt:
                kw["initial_prompt"] = prompt
            hot = getattr(config, "STT_HOTWORDS", "") or ""
            if hot:
                kw["hotwords"] = hot
            try:
                segments, _ = self.model.transcribe(
                    audio, language=config.STT_LANGUAGE, vad_filter=True,
                    condition_on_previous_text=False, **kw)
            except TypeError:
                kw.pop("hotwords", None)
                segments, _ = self.model.transcribe(
                    audio, language=config.STT_LANGUAGE, vad_filter=True,
                    condition_on_previous_text=False, **kw)
            text = _collapse_repeats(" ".join(s.text for s in segments).strip())
            if _echoes_prompt(text, prompt):
                if os.environ.get("REEDUS_DEBUG"):
                    print(f"[stt] discarded prompt echo: {text!r}", file=sys.stderr)
                return ""
            self._checked = True
            return canon_name(text)
        except Exception as e:
            msg = str(e).lower()
            if self.device == "cuda" and not self._checked and (
                    "cublas" in msg or "cudnn" in msg or "cuda" in msg or "library" in msg):
                self._fallback_to_cpu()
                self._checked = True
                return self.transcribe(audio)
            raise


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(STT().transcribe(sys.argv[1]))
    else:
        print("usage: python -m reedus_companion.voice.stt <audiofile>")
