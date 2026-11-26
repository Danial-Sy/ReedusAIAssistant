"""audio in and out of the relay.

    Endpointer   when did they start and stop talking. a real speech detector (Silero VAD,
                 relay/models/silero_vad.onnx) so a fan, a TV murmur or the room's hum doesn't
                 keep the turn open; plain energy only as a fallback (and in the tests)
    transcribe   speech to text: Gemini 3.5 Transcribe (the same Gemini key as the brain), older
                 Gemini models as a fallback, or OpenAI
    Voice        Inworld, streamed and resampled to the device's 16 kHz 20 ms frames

everything here is blocking; session.py runs it in worker threads.
"""
from __future__ import annotations

import io
import math
import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Iterator, Optional

import numpy as np

from reedus_relay import config

FRAME_BYTES = config.FRAME_SAMPLES * 2
FRAME_S = config.FRAME_SAMPLES / config.RATE


def level_db(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return -120.0
    return 20.0 * math.log10(max(1e-6, float(np.sqrt(np.mean(x * x)))))


# ---------- the speech detector ----------
_SILERO_PATH = Path(__file__).resolve().parents[1] / "models" / "silero_vad.onnx"
_silero_lock = threading.Lock()
_silero_session = None
_silero_failed = ""


def _silero():
    """The shared ONNX session (one per process; each Endpointer keeps its own state)."""
    global _silero_session, _silero_failed
    if _silero_session is not None or _silero_failed:
        return _silero_session
    with _silero_lock:
        if _silero_session is None and not _silero_failed:
            try:
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.inter_op_num_threads = 1
                opts.intra_op_num_threads = 1
                path = Path(config.env("RELAY_VAD_MODEL", str(_SILERO_PATH)))
                _silero_session = ort.InferenceSession(str(path), sess_options=opts,
                                                       providers=["CPUExecutionProvider"])
            except Exception as e:                                # noqa: BLE001
                _silero_failed = f"{type(e).__name__}: {e}"
                print(f"[relay] speech detector unavailable ({_silero_failed}); "
                      f"falling back to loudness, which is much worse in a noisy room", flush=True)
    return _silero_session


def vad_status() -> str:
    if config.VAD == "energy":
        return "loudness (RELAY_VAD=energy)"
    return "Silero" if _silero() is not None else f"loudness (Silero failed: {_silero_failed})"


class SileroVad:
    """Silero VAD v5 on 16 kHz audio: 512-sample windows with 64 samples of context.
    Feed any number of samples; prob() is the latest window's speech probability."""
    WIN, CTX = 512, 64

    def __init__(self, session):
        self.s = session
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros(self.CTX, dtype=np.float32)
        self.buf = np.zeros(0, dtype=np.float32)
        self.sr = np.array(config.RATE, dtype=np.int64)
        self.p = 0.0

    def feed(self, pcm: bytes) -> float:
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self.buf = np.concatenate([self.buf, x])
        while self.buf.size >= self.WIN:
            win, self.buf = self.buf[:self.WIN], self.buf[self.WIN:]
            inp = np.concatenate([self.ctx, win])[None, :]
            out, self.state = self.s.run(None, {"input": inp, "state": self.state, "sr": self.sr})
            self.ctx = win[-self.CTX:]
            self.p = float(out[0][0])
        return self.p


# ---------- endpointing ----------
class Endpointer:
    """Feed 20 ms int16 frames. Events:

        start    they started talking
        pause    they've been quiet for SPEC_S: a good moment to start transcribing early
        resume   ...and then went on talking (throw that early transcript away)
        end      quiet for END_SILENCE_S after real speech: the sentence is over
        timeout  nothing said within start_timeout
        max      talked for MAX_UTTERANCE_S

    With Silero a frame counts as speech when the detector says so AND it's within LOCK_DB of
    the loudest speech this turn: the person talking to Lite is close and loud, a TV or someone
    across the room is much quieter, so they can't hold the turn open on their own.
    """

    def __init__(self, start_timeout: float = config.START_TIMEOUT_S,
                 end_silence: float = config.END_SILENCE_S,
                 max_len: float = config.MAX_UTTERANCE_S, noise_db: float = -60.0,
                 vad: Optional[str] = None, spec_after: float = config.SPEC_S,
                 floor_db: float = -120.0):
        self.start_timeout, self.end_silence, self.max_len = start_timeout, end_silence, max_len
        self.spec_after = spec_after
        self.floor_db = floor_db     # quieter than this never counts (a TV while he's thinking)
        self.carry = b""             # audio from before (a sentence that's being continued)
        self.guard_s, self.guard_db = 0.0, -120.0   # for the first guard_s, quieter than
                                                    # guard_db never counts (his echo)
        mode = (vad or config.VAD or "silero").lower()
        sess = _silero() if mode == "silero" else None
        self.vad = SileroVad(sess) if sess is not None else None
        self.mode = "silero" if self.vad is not None else "energy"
        self.noise = noise_db
        self.t = 0.0
        self.started = False
        self.voiced_run = 0
        self.speech_run = 0.0        # seconds of speech before 'start' (silero)
        self.silence = 0.0
        self.speech_t = 0.0
        self.voiced_t = 0.0          # seconds that were actually speech, after start
        self.peak = -120.0           # loudest speech frame this turn (dB)
        self.in_speech = False
        self.paused = False
        self.frames: list = []
        self.levels: list = []

    # kept for anyone calling it the old way
    level_db = staticmethod(level_db)

    def threshold(self) -> float:
        return max(self.noise + 12.0, -48.0)

    def _voiced(self, pcm: bytes, db: float) -> bool:
        if self.t <= self.guard_s and db < self.guard_db:
            if self.vad is not None:
                self.vad.feed(pcm)                       # keep the detector's state moving
            return False
        if self.vad is None:
            voiced = db > self.threshold()
            # the floor follows the room down fast and up slowly, and it learns from every
            # frame, not just the quiet ones: a room that's louder than the threshold from the
            # start used to look like someone talking forever
            if not voiced or db < self.noise + 20:
                self.noise = db if db < self.noise else self.noise + 0.02 * (db - self.noise)
            elif not self.started:
                self.noise += 0.004 * (db - self.noise)
            return voiced
        p = self.vad.feed(pcm)
        # hysteresis: harder to start than to keep going
        self.in_speech = p >= (config.VAD_KEEP if self.in_speech else config.VAD_ON)
        if not self.in_speech:
            return False
        if db < -58.0 or db < self.floor_db:             # whisper-quiet: not someone at Lite
            return False
        if self.started and db < self.peak - config.LOCK_DB:
            return False                                 # a quieter voice in the background
        return True

    def feed(self, pcm: bytes) -> Optional[str]:
        self.t += FRAME_S
        db = level_db(pcm)
        voiced = self._voiced(pcm, db)
        if voiced:
            self.peak = max(self.peak, db) if self.started else max(self.peak, db - 3.0)
        if self.started or voiced:
            self.frames.append(pcm)
        if not self.started:
            if self.mode == "silero":
                self.speech_run = self.speech_run + FRAME_S if voiced else max(0.0, self.speech_run - FRAME_S)
                go = self.speech_run >= config.VAD_START_S
            else:
                self.voiced_run = self.voiced_run + 1 if voiced else 0
                go = self.voiced_run >= 3
            if go:
                self.started = True
                self.frames = self.frames[-25:] if self.mode == "silero" else self.frames[-6:]
                return "start"
            if self.t >= self.start_timeout:
                return "timeout"
            if not voiced:
                self.frames = self.frames[-25:] if self.mode == "silero" else self.frames[-5:]
            return None
        self.speech_t += FRAME_S
        if voiced:
            self.voiced_t += FRAME_S
            self.silence = 0.0
            if self.paused:
                self.paused = False
                return "resume"
        else:
            self.silence += FRAME_S
        if self.silence >= self.end_silence:
            if self.mode == "silero" and self.voiced_t < config.VAD_MIN_SPEECH_S:
                # a cough or a door: not a sentence. start over, same deadline
                self._restart()
                return None
            return "end"
        if not self.paused and self.spec_after and self.silence >= self.spec_after:
            self.paused = True
            return "pause"
        if self.speech_t >= self.max_len:
            return "max"
        return None

    def _restart(self) -> None:
        self.started = False
        self.speech_run = 0.0
        self.voiced_run = 0
        self.silence = self.speech_t = self.voiced_t = 0.0
        self.paused = False
        self.peak = -120.0
        self.frames = []

    def audio(self) -> bytes:
        return self.carry + b"".join(self.frames)


def wav_bytes(pcm: bytes, rate: int = config.RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------- speech to text ----------
_STT_PROMPT = ("Transcribe exactly what the speaker says, in their language. Output only the "
               "words, no quotes, no labels. If there is no intelligible speech, output nothing. "
               "They are talking to a robot named Reedus (said REE-dus). When they say that name, "
               "write it as Reedus, not Rita, Frita, Reeta or Redis.")
# USD per minute of audio for the transcription-only models (list prices, estimates)
_STT_PER_MIN = {"gpt-4o-mini-transcribe": 0.003, "gpt-4o-transcribe": 0.006, "whisper-1": 0.006}

_clients: dict = {}


def stt_provider() -> str:
    """Who turns speech into text: RELAY_STT if set, else Gemini if there's a Gemini key, else
    OpenAI if there's an OpenAI key. (Claude and the rest can't take audio.)"""
    explicit = config.env("RELAY_STT").lower()
    if explicit in ("gemini", "openai"):
        return explicit
    from reedus_companion.cloud import llm_settings
    if not llm_settings.key("gemini") and llm_settings.key("openai"):
        return "openai"
    return "gemini"


def stt_ready() -> bool:
    from reedus_companion.cloud import llm_settings
    return bool(llm_settings.key(stt_provider()))


def transcribe(pcm: bytes, provider: Optional[str] = None, quick: bool = False) -> str:
    """Text of what was said, '' if nothing. Costs land on the current meter. quick: a check
    for "stop" or his name while he talks (a lighter model first, see _stt_models)."""
    provider = provider or stt_provider()
    if len(pcm) < FRAME_BYTES * 10:          # under 200 ms is a cough, not a sentence
        return ""
    if provider == "openai":
        return clean_transcript(_openai_stt(pcm), pcm)
    return clean_transcript(_gemini_stt(pcm, quick=quick), pcm)


def transcribe_quick(pcm: bytes) -> str:
    return transcribe(pcm, quick=True)


# what speech-to-text models make up when they're handed noise
_HALLUCINATED = {"thank you", "thanks", "thank you.", "thanks for watching", "you", "bye",
                 "okay", "ok", "hmm", "mm", "uh", "um", "[silence]", "(silence)", "silence",
                 "[inaudible]", "(inaudible)", "[music]", "(music)", "[noise]", "..."}


# what speech-to-text writes when he hears "Reedus" later in a sentence (the wake word itself is
# fine, it runs on the S3). names nobody else has are always fixed; real names and words (Rita,
# Redis, "read us") only where they're plainly said TO him, so a real Rita in a story survives
_NAME_ODD = r"(?:frita'?s?|freeta'?s?|freedus|freedas|reedis|reedas|redus|reidus|ridus|reetus|reedus)"
_NAME_REAL = r"(?:rita'?s?|reeta'?s?|reda'?s?|redis|reed\s+us|read\s+us)"
_HAIL = r"(?:hey|hi|hello|ok(?:ay)?|yo|thanks|thank\s+you|bye|goodbye|goodnight|good\s+night|morning|good\s+morning|sorry)"
_NAME_FIXES = (
    (re.compile(rf"\b{_NAME_ODD}\b", re.I), "Reedus"),
    (re.compile(rf"\b({_HAIL},?\s+){_NAME_REAL}\b", re.I), r"\1Reedus"),      # "hey Rita", "thanks, Rita"
    (re.compile(rf"^(\s*){_NAME_REAL}(?=\s*[,.!?]|\s*$)", re.I), r"\1Reedus"), # "Rita, what's..."
    (re.compile(rf"(,\s*){_NAME_REAL}(?=\s*[.!?]?\s*$)", re.I), r"\1Reedus"),  # "...right, Rita?"
)


def fix_name(text: str) -> str:
    """'Rita', 'Frita', 'Rita's' etc. back to Reedus where they mean him."""
    for rx, to in _NAME_FIXES:
        text = rx.sub(to, text)
    return text


def clean_transcript(text: str, pcm: bytes) -> str:
    t = (text or "").strip().strip('"').strip()
    low = t.lower().strip(" .!?")
    if not t or low in ("", "[no speech]", "no speech", "(no speech)"):
        return ""
    secs = len(pcm) / 2 / config.RATE
    if low in _HALLUCINATED and secs < 1.6:
        return ""
    if t.startswith(("[", "(")) and t.endswith(("]", ")")) and len(t) < 30:
        return ""                             # [background noise], (coughs)
    return fix_name(t)


# models that can't be used for this key (unknown id, no access), learned at runtime
_STT_DEAD: set = set()
# models over their per-minute quota (429), and until when. gemini-3.5-transcribe on Tier 1 is
# 10 requests a minute, and early transcripts plus "stop" checks went past that on 11-24: every
# call then failed (after the SDK's own retries) instead of moving on to Flash-Lite
_STT_COOL: dict = {}
_STT_MODELS = ("gemini-3.5-transcribe", "gemini-3.5-flash-lite", "gemini-3.7-flash")


def _stt_models(quick: bool = False) -> list:
    explicit = config.env("RELAY_STT_MODEL")
    if explicit:
        return [explicit]
    now = time.monotonic()
    # a dedicated speech-to-text model first (fast, no thinking), then a general Flash.
    # quick (checking for "stop" / his name over him): Flash-Lite first, so those checks don't
    # spend the transcribe model's small per-minute quota that real turns need
    order = (_STT_MODELS[1], _STT_MODELS[0], _STT_MODELS[2]) if quick else _STT_MODELS
    usable = [m for m in order if m not in _STT_DEAD]
    ready = [m for m in usable if _STT_COOL.get(m, 0.0) <= now]
    return ready or usable


def _quota_hit(e: BaseException) -> float:
    """Seconds to leave a model alone after a 429 (its own "retry in Ns", 5-90 s), else 0."""
    msg = str(e)
    if not any(w in msg for w in ("429", "RESOURCE_EXHAUSTED", "rate limit", "quota")):
        return 0.0
    import re
    m = re.search(r"retry in ([0-9.]+)s", msg) or re.search(r"retryDelay'?:\s*'?([0-9.]+)s", msg)
    return max(5.0, min(90.0, float(m.group(1)) if m else 30.0))


def _gemini_stt(pcm: bytes, quick: bool = False) -> str:
    from google import genai
    from google.genai import types
    from reedus_companion.cloud import budget, llm_settings
    key = llm_settings.key("gemini")
    if not key:
        raise RuntimeError("to understand speech the relay needs a Gemini or OpenAI key (Settings → Service keys)")
    # gemini-3.5-transcribe only exists on v1beta; google-genai 3.x defaults to v1, which 404'd
    # it on every turn and quietly fell back to Flash-Lite (no custom vocabulary, so "Rita")
    ver = config.env("RELAY_STT_API_VERSION", "v1beta")
    client = _clients.get(("gemini-stt", key, ver))
    if client is None:
        try:                                 # no SDK retries: a 429 moves to the next model
            opts = types.HttpOptions(api_version=ver, retry_options=types.HttpRetryOptions(attempts=1))
        except Exception:                                        # noqa: BLE001
            opts = types.HttpOptions(api_version=ver)
        client = _clients[("gemini-stt", key, ver)] = genai.Client(api_key=key, http_options=opts)
    audio = types.Part.from_bytes(data=wav_bytes(pcm), mime_type="audio/wav")
    last: Optional[BaseException] = None
    for model in _stt_models(quick) or ["gemini-3.7-flash"]:
        t0 = time.perf_counter()
        try:
            resp = _gemini_stt_call(client, types, model, audio)
        except Exception as e:                                   # noqa: BLE001
            last = e
            cool = _quota_hit(e)
            if cool:
                if _STT_COOL.get(model, 0.0) <= time.monotonic():
                    print(f"[relay] speech-to-text: {model} is over its per-minute quota; using the "
                          f"next model for {cool:.0f} s", flush=True)
                _STT_COOL[model] = time.monotonic() + cool
                continue
            msg = str(e).lower()
            if any(w in msg for w in ("not found", "404", "not supported", "permission",
                                      "unsupported", "does not exist", "invalid argument")):
                print(f"[relay] speech-to-text: {model} can't be used ({type(e).__name__}: "
                      f"{str(e)[:160]}); trying the next model", flush=True)
                _STT_DEAD.add(model)
                continue
            raise
        text = (getattr(resp, "text", "") or "").strip()
        u = getattr(resp, "usage_metadata", None)
        n_in = int(getattr(u, "prompt_token_count", 0) or 0) or int(len(pcm) / 2 / config.RATE * 32) + 40
        n_out = int(getattr(u, "candidates_token_count", 0) or 0) or len(text) // 4
        try:
            budget.record_tokens(model, n_in, n_out)
        except Exception:                                        # noqa: BLE001
            pass
        global last_stt
        last_stt = {"model": model, "ms": int((time.perf_counter() - t0) * 1000)}
        return text
    raise last or RuntimeError("no speech-to-text model worked")


last_stt: dict = {}


def _gemini_stt_call(client, types, model: str, audio):
    if "transcribe" in model:
        cfg = None
        try:
            # his name, so "Reedus" doesn't come back as "Redis" or "reed us"
            cfg = types.GenerateContentConfig(
                audio_transcription_config=types.AudioTranscriptionConfig(
                    custom_vocabulary=["Reedus", "Reedus Lite"]))
        except Exception:                                        # noqa: BLE001
            cfg = None                       # an SDK without the field: the plain call works too
        try:
            return client.models.generate_content(model=model, contents=[audio], config=cfg)
        except Exception as e:                                   # noqa: BLE001
            if cfg is None or "transcription" not in str(e).lower():
                raise
            return client.models.generate_content(model=model, contents=[audio])
    contents = [audio, _STT_PROMPT]
    level = "minimal" if "lite" in model else "low"
    try:
        return client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(max_output_tokens=256,
                                               thinking_config=types.ThinkingConfig(thinking_level=level)))
    except Exception as e:                                       # noqa: BLE001
        if "thinking" not in str(e).lower():
            raise
        return client.models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(max_output_tokens=256))


def _openai_stt(pcm: bytes) -> str:
    import openai
    from reedus_companion.cloud import llm_settings
    from reedus_relay import meter
    key = llm_settings.key("openai")
    if not key:
        raise RuntimeError("to understand speech the relay needs a Gemini or OpenAI key (Settings → Service keys)")
    client = _clients.get(("openai", key))
    if client is None:
        client = _clients[("openai", key)] = openai.OpenAI(api_key=key, max_retries=1, timeout=20)
    model = config.env("RELAY_STT_MODEL", "gpt-4o-mini-transcribe")
    out = client.audio.transcriptions.create(model=model, file=("speech.wav", wav_bytes(pcm), "audio/wav"))
    m = meter.current()
    if m is not None:
        m.add(len(pcm) / 2 / config.RATE / 60.0 * _STT_PER_MIN.get(model, 0.006), model)
    return (getattr(out, "text", "") or "").strip()


# ---------- voice ----------
# the Inworld models an account can pick in the app (Settings → Voice): quality or speed
VOICE_MODELS = [
    {"id": "inworld-tts-2", "label": "Quality",
     "note": "Most natural. About a second slower to start."},
    {"id": "inworld-tts-2-flash", "label": "Fast",
     "note": "A second quicker, half the cost. Flatter, and skips mode speaking styles."},
]
_VOICE_MODEL_IDS = {m["id"] for m in VOICE_MODELS}


def default_voice_model() -> str:
    m = (os.environ.get("REEDUS_INWORLD_MODEL") or "").strip()
    return m if m in _VOICE_MODEL_IDS else "inworld-tts-2"


def voice_model(prefs: Optional[dict]) -> str:
    m = ((prefs or {}).get("model") or "").strip()
    return m if m in _VOICE_MODEL_IDS else default_voice_model()


class Voice:
    """Inworld (reedus_companion.voice.tts_inworld), resampled to the device's frames."""

    def __init__(self, tts=None, voice: Optional[str] = None, style: Optional[str] = None,
                 speed: Optional[float] = None, model: Optional[str] = None):
        if tts is None:
            from reedus_companion.voice.tts_inworld import InworldTTS
            tts = InworldTTS(voice=voice or None, style=style)
            if speed:
                tts._rate = max(0.5, min(1.5, float(speed)))
            if model and model != tts.model:
                tts.model = model
                # Flash has no natural-language steering (tts_inworld decides the same way)
                tts._steer = bool(tts.style) and model.startswith("inworld-tts-2")                     and not model.endswith("flash")
        self.tts = tts
        self.rate = int(getattr(tts, "sample_rate", 24000))

    def warm(self) -> None:
        """Open the TLS connection now (cold it costs ~250 ms on the first sentence)."""
        try:
            s = getattr(self.tts, "_session", None)
            if s is not None:
                s.get("https://api.inworld.ai/tts/v1/voices?filter=language%3Den", timeout=5)
        except Exception:                                        # noqa: BLE001
            pass

    def frames(self, text: str) -> Iterator[bytes]:
        """640-byte 16 kHz int16 frames for `text`, as they're synthesised."""
        from reedus_companion.lite.resample import Resampler, to_int16
        rs = Resampler(self.rate, config.RATE)
        carry = b""
        stream = self.tts.synth_stream(text) if hasattr(self.tts, "synth_stream") \
            else iter([self.tts.synth(text)])
        for chunk in stream:
            pcm = carry + to_int16(rs.feed(np.asarray(chunk, dtype=np.float32)))
            n = len(pcm) // FRAME_BYTES * FRAME_BYTES
            for i in range(0, n, FRAME_BYTES):
                yield pcm[i:i + FRAME_BYTES]
            carry = pcm[n:]
        if carry:
            yield carry + b"\x00" * (FRAME_BYTES - len(carry))


# one Inworld client per (voice, style, speed, keys version), shared by every thread: a new
# client per worker thread meant a fresh TLS handshake on many sentences
_voices: dict = {}
_voices_lock = threading.Lock()


def voice_for(prefs: Optional[dict] = None) -> Voice:
    from reedus_relay import vault
    p = prefs or {}
    key = (p.get("id") or "", p.get("style"), float(p.get("speed") or 0) or None,
           voice_model(p), vault.VERSION)
    with _voices_lock:
        v = _voices.get(key)
        if v is None:
            v = _voices[key] = Voice(voice=key[0] or None, style=key[1], speed=key[2], model=key[3])
        return v


# Inworld's built-in English voices, only for when their list can't be fetched; the live list
# from the API (with each voice's description) wins whenever it answers
FALLBACK_VOICES = [{"id": n, "name": n, "desc": ""} for n in (
    "Theodore", "Dennis", "Alex", "Craig", "Edward", "Mark", "Ronald", "Shaun", "Timothy", "Hades",
    "Ashley", "Deborah", "Elizabeth", "Julia", "Olivia", "Priya", "Sarah", "Wendy", "Pixie")]
FALLBACK_VOICES[0]["desc"] = "the voice Reedus started with"
_voice_list: dict = {"at": 0.0, "rows": None}


def list_voices() -> dict:
    """{"voices": [{id, name, desc}], "source": "inworld" | "builtin"}, cached for an hour."""
    if _voice_list["rows"] and time.time() - _voice_list["at"] < 3600:
        return {"voices": _voice_list["rows"], "source": "inworld"}
    key = (os.environ.get("INWORLD_API_KEY") or "").strip()
    if key:
        try:
            import requests
            r = requests.get("https://api.inworld.ai/tts/v1/voices",
                             params={"filter": "language=en"},
                             headers={"Authorization": f"Basic {key}"}, timeout=8)
            if r.status_code < 400:
                rows = []
                for v in (r.json() or {}).get("voices") or []:
                    vid = v.get("voiceId") or v.get("voice_id") or v.get("name") or ""
                    if not vid:
                        continue
                    langs = v.get("languages") or []
                    if langs and not any(str(l).lower().startswith("en") for l in langs):
                        continue
                    desc = v.get("description") or ", ".join(v.get("tags") or [])
                    rows.append({"id": vid, "name": v.get("displayName") or vid, "desc": desc[:90]})
                if rows:
                    rows.sort(key=lambda x: x["name"].lower())
                    _voice_list.update(at=time.time(), rows=rows)
                    return {"voices": rows, "source": "inworld"}
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] couldn't list Inworld voices: {type(e).__name__}: {e}", flush=True)
    return {"voices": FALLBACK_VOICES, "source": "builtin"}


def preview_wav(prefs: dict, text: str) -> bytes:
    """A short sample in that voice, as a 24 kHz WAV for the browser."""
    v = Voice(voice=prefs.get("id") or None, style=prefs.get("style"),
              speed=float(prefs.get("speed") or 0) or None, model=voice_model(prefs))
    audio = v.tts.synth(text)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    return wav_bytes(pcm, rate=v.rate)


def tone(freq: float = 880.0, secs: float = 0.12, level: float = 0.25) -> bytes:
    """A short beep at the wire rate, for 'I heard you' and errors."""
    n = int(config.RATE * secs)
    t = np.arange(n) / config.RATE
    env = np.minimum(1.0, np.minimum(t, secs - t) / 0.01)
    x = level * env * np.sin(2 * np.pi * freq * t)
    return (x * 32767).astype("<i2").tobytes()


def wake_detector(caps=None):
    """One Lite's wake word: its own ("wake" in its hello caps), openWakeWord, or a stand-in,
    as RELAY_WAKE says (see wake.py)."""
    from reedus_relay.wake import make
    return make(os.environ.get("RELAY_WAKE", "device"), caps)


def wake_status() -> tuple:
    """(ready, description) of the relay's wake word setup, for the website's checklist."""
    from reedus_relay.wake import status
    return status(os.environ.get("RELAY_WAKE", "device"))
