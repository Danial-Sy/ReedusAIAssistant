"""who's talking: a voiceprint from a clip of speech, and which saved person it sounds like.

    the model   CAM++ trained on VoxCeleb (WeSpeaker, the ONNX export from sherpa-onnx's
                releases). 80 log-mel features in, a 512-number voiceprint out. about 30 ms
                for a 4 s clip on one CPU core, so it runs next to speech-to-text and adds
                nothing to the wait. the Dockerfile downloads it into relay/models/.
    a person    the average of their voiceprints (one per sentence they said while the app was
                teaching him their voice, plus a few from later turns he was sure about).
    matching    cosine similarity against every person on the account. checked on real speech
                (LibriSpeech readers, 4 s clips, a voice averaged from 3 clips vs a 4th): same
                person 0.70-0.88 clean and still 0.60+ with room echo and noise added (down
                to 6 dB SNR); different people at most 0.34 clean, but noise drags everyone
                together (0.43 at 12 dB, 0.50 at 6 dB). so the cutoff is 0.50, and the best
                match also has to beat the runner-up by a margin. RELAY_SPK_MATCH tunes it:
                every turn logs the scores ("voice: Dan (voice, 2.1 s ...; Dan 0.71, Mom 0.22)").

Pitch alone can't do this (two people's ranges overlap and it moves with mood); the model
looks at the whole spectrum of how someone sounds.
"""
from __future__ import annotations

import base64
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np

from reedus_relay import config

MODEL_NAME = "wespeaker_en_voxceleb_CAM++.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
             "speaker-recongition-models/wespeaker_en_voxceleb_CAM++.onnx")
MODEL_SHA256 = "7acd352a0372de23ce49379b02864833b7f3f501b4ea25248fc977389f08e5fc"
MODEL_ID = "campp-vox-1"         # stored with each voiceprint: a new model means re-teaching

FRAME = 320                      # 20 ms at 16 kHz
MIN_EMBED_S = 0.4                # less speech than this gives a voiceprint too noisy to use


def model_path() -> Path:
    explicit = config.env("RELAY_SPK_MODEL")
    if explicit:
        return Path(explicit).expanduser()
    for cand in (Path(__file__).resolve().parents[1] / "models" / MODEL_NAME,
                 config.DATA / MODEL_NAME):
        if cand.exists():
            return cand
    return Path(__file__).resolve().parents[1] / "models" / MODEL_NAME


# ---------- the model ----------
_lock = threading.Lock()
_state: dict = {}
_override: Optional[Callable[[np.ndarray], np.ndarray]] = None   # tests


def use_embedder(fn: Optional[Callable[[np.ndarray], np.ndarray]]) -> None:
    """Tests: fn(samples as float32 in int16 scale) -> voiceprint. None = the real model."""
    global _override
    _override = fn


def _load():
    with _lock:
        if "sess" in _state or "error" in _state:
            return _state.get("sess")
        path = model_path()
        try:
            import kaldi_native_fbank  # noqa: F401
            import onnxruntime as ort
            if not path.exists():
                raise FileNotFoundError(f"{path} (the Dockerfile downloads it)")
            so = ort.SessionOptions()
            so.intra_op_num_threads = 1          # one turn at a time per Lite; leave cores free
            so.inter_op_num_threads = 1
            _state["sess"] = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])
            print(f"[relay] voice recognition ready ({path.name})", flush=True)
        except Exception as e:                                   # noqa: BLE001
            _state["error"] = f"{type(e).__name__}: {e}"
            print(f"[relay] *** voice recognition off: {_state['error']}", flush=True)
        return _state.get("sess")


def available() -> bool:
    if config.env("RELAY_SPEAKERS", "on").lower() in ("off", "0", "no"):
        return False
    return _override is not None or _load() is not None


def why_not() -> str:
    if config.env("RELAY_SPEAKERS", "on").lower() in ("off", "0", "no"):
        return "turned off on this relay (RELAY_SPEAKERS=off)"
    return _state.get("error", "")


def _features(x: np.ndarray) -> np.ndarray:
    """Kaldi fbank exactly as WeSpeaker trained on: 80 bins, 25/10 ms hamming frames, no
    dither, samples in int16 scale, then each bin's mean taken off (CMN)."""
    import kaldi_native_fbank as knf
    o = knf.FbankOptions()
    o.frame_opts.dither = 0
    o.frame_opts.samp_freq = config.RATE
    o.frame_opts.snip_edges = True
    o.frame_opts.window_type = "hamming"
    o.mel_opts.num_bins = 80
    fb = knf.OnlineFbank(o)
    fb.accept_waveform(config.RATE, x.tolist())
    fb.input_finished()
    f = np.stack([fb.get_frame(i) for i in range(fb.num_frames_ready)]).astype(np.float32)
    return f - f.mean(axis=0, keepdims=True)


def _run(x: np.ndarray) -> np.ndarray:
    if _override is not None:
        return np.asarray(_override(x), dtype=np.float32)
    sess = _load()
    if sess is None:
        raise RuntimeError("voice recognition isn't available")
    feats = _features(x)[None]
    return sess.run(None, {sess.get_inputs()[0].name: feats})[0][0].astype(np.float32)


# ---------- audio ----------
def speech_only(pcm: bytes) -> Tuple[np.ndarray, float]:
    """The speech in a clip, silence cut out (it only dilutes a voiceprint), and how many
    seconds of speech that is. A frame counts when it's within 30 dB of the loudest one and
    above -55 dBFS; one frame either side of each run is kept."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32)
    n = len(x) // FRAME
    if n == 0:
        return x[:0], 0.0
    fr = x[: n * FRAME].reshape(n, FRAME)
    db = 10 * np.log10(np.mean(fr ** 2, axis=1) / (32768.0 ** 2) + 1e-12)
    keep = (db > max(db.max() - 30.0, -55.0))
    keep = keep | np.roll(keep, 1) | np.roll(keep, -1)
    voiced = fr[keep].reshape(-1)
    return voiced, float(keep.sum()) * FRAME / config.RATE


def embed(pcm: bytes) -> Optional[Tuple[np.ndarray, float]]:
    """(unit-length voiceprint, seconds of speech it came from), or None for too little speech."""
    x, secs = speech_only(pcm)
    if secs < MIN_EMBED_S:
        return None
    v = _run(x)
    norm = float(np.linalg.norm(v))
    if not np.isfinite(norm) or norm == 0:
        return None
    return v / norm, secs


def encode(v: np.ndarray) -> str:
    return base64.b64encode(np.asarray(v, dtype="<f4").tobytes()).decode()


def decode(s: str) -> Optional[np.ndarray]:
    try:
        v = np.frombuffer(base64.b64decode(s), dtype="<f4").astype(np.float32)
    except Exception:                                            # noqa: BLE001
        return None
    return v if v.size else None


def average(vs: List[np.ndarray]) -> Optional[np.ndarray]:
    vs = [v for v in vs if v is not None and v.size]
    if not vs:
        return None
    size = vs[0].size
    vs = [v for v in vs if v.size == size]
    c = np.mean(np.stack(vs), axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n else None


def consistent(vs: List[np.ndarray], floor: float = 0.30) -> List[np.ndarray]:
    """Drop the sentences that don't sound like the rest (someone else chimed in during the
    teaching, or it was mostly noise). Needs three to judge."""
    if len(vs) < 3:
        return vs
    keep = []
    for i, v in enumerate(vs):
        rest = average([w for j, w in enumerate(vs) if j != i])
        if rest is None or float(v @ rest) >= floor:
            keep.append(v)
    return keep if len(keep) >= 2 else vs


# ---------- deciding who it is ----------
def match_cutoff() -> float:
    return config.env_float("RELAY_SPK_MATCH", 0.50)


def margin() -> float:
    return config.env_float("RELAY_SPK_MARGIN", 0.08)


def sure_cutoff() -> float:
    return config.env_float("RELAY_SPK_SURE", 0.62)


def min_seconds() -> float:
    return config.env_float("RELAY_SPK_MIN_S", 0.8)


@dataclass
class Match:
    voice_id: str = ""               # "" = not recognized
    score: float = 0.0               # similarity to the person picked (or the best, if nobody)
    second: float = -1.0             # the runner-up's
    secs: float = 0.0                # seconds of speech it was judged on
    how: str = ""                    # voice | kept | short | unsure | unknown | quiet | none
    scores: dict = field(default_factory=dict)
    vec: Optional[np.ndarray] = None

    @property
    def learnable(self) -> bool:
        """Sure enough to fold this clip into the person's voiceprint."""
        return (self.how == "voice" and self.score >= sure_cutoff() and self.secs >= 2.0
                and self.score - self.second >= 0.15)


def decide(vec: Optional[np.ndarray], secs: float, people: List[Tuple[str, np.ndarray]],
           sticky: str = "") -> Match:
    """people: (voice id, average voiceprint). sticky: who was talking a moment ago in this
    conversation, the fallback when this clip is too short or too close to call."""
    if not people:
        return Match(how="none", secs=secs)
    if vec is None:
        return Match(voice_id=sticky, how="kept" if sticky else "quiet", secs=secs)
    scores = {vid: float(vec @ c) for vid, c in people if c is not None and c.size == vec.size}
    if not scores:
        return Match(how="none", secs=secs, vec=vec)
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, s1 = ranked[0]
    s2 = ranked[1][1] if len(ranked) > 1 else -1.0
    base = dict(secs=secs, scores=scores, vec=vec, second=s2)
    cut = match_cutoff() + (0.10 if secs < min_seconds() else 0.0)   # a short clip needs more
    if s1 >= cut and s1 - s2 >= margin():
        return Match(voice_id=best, score=s1, how="voice", **base)
    if sticky in scores:
        ss = scores[sticky]
        near = ss >= match_cutoff() - 0.12
        if secs < min_seconds() or (near and (sticky == best or s1 - ss < margin())):
            # too short to judge, or close: whoever was talking is still the best bet
            return Match(voice_id=sticky, score=ss, how="kept", **base)
    how = "short" if secs < min_seconds() else ("unsure" if s1 >= match_cutoff() else "unknown")
    return Match(score=s1, how=how, **base)
