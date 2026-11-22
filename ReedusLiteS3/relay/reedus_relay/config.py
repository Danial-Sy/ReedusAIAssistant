"""relay settings, all from the environment (Railway → service → Variables).

    DATABASE_URL            postgres://… from Railway's Postgres plugin. unset = SQLite at
                            RELAY_DATA/relay.sqlite (local runs and tests)
    PORT                    Railway sets it. default 8080
    RELAY_ADMIN_TOKEN       the setup code: the website asks for it to set the relay up, and the
                            owner can always sign in with it. also guards tools/admin.py
    RELAY_DATA              scratch dir for SQLite and models. default ./data

    keys the relay spends from (never sent to devices):
    GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY / … (reedus_companion/cloud/providers.py)
    INWORLD_API_KEY         his voice

    defaults an account can override on the website:
    REEDUS_LLM_PROVIDER / REEDUS_LLM_MODEL / REEDUS_LLM_SEARCH   as on the run box
    RELAY_STT               gemini | openai. default gemini (one key fewer)
    RELAY_STT_MODEL         default gemini-3.7-flash / gpt-4o-mini-transcribe
    REEDUS_WAKEWORD_MODEL   path to hey_reedus.onnx (see README)
"""
from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


DATA = Path(env("RELAY_DATA", str(Path.cwd() / "data"))).expanduser()


def _ui_dir() -> Path:
    """The website the relay serves at / (ReedusLiteS3/web; Docker copies it to /app/ui)."""
    explicit = env("RELAY_UI")
    if explicit:
        return Path(explicit).expanduser()
    for cand in (Path("/app/ui"), Path(__file__).resolve().parents[2] / "web"):
        if (cand / "index.html").exists():
            return cand
    return Path("/nonexistent")


UI_DIR = _ui_dir()
RELAY_NAME = env("RELAY_NAME", "Reedus relay")
PORT = int(env("PORT", "8080"))
HOST = env("RELAY_HOST", "0.0.0.0")
ADMIN_TOKEN = env("RELAY_ADMIN_TOKEN")
DATABASE_URL = env("DATABASE_URL")

# audio, same wire format as the run box (ReedusLite/PROTOCOL.md)
RATE = 16000
FRAME_SAMPLES = 320
LEAD_S = env_float("RELAY_LEAD_S", 0.35)          # speech sent ahead of the device's playhead
PREBUFFER_S = 0.12

# listening
START_TIMEOUT_S = env_float("RELAY_START_TIMEOUT_S", 6.0)    # wake, then nothing said
END_SILENCE_S = env_float("RELAY_END_SILENCE_S", 0.6)        # pause that ends a sentence
MAX_UTTERANCE_S = env_float("RELAY_MAX_UTTERANCE_S", 15.0)
FOLLOW_UP_S = env_float("RELAY_FOLLOW_UP_S", 6.0)            # answer without the wake word
HISTORY_TURNS = int(env_float("RELAY_HISTORY_TURNS", 12))    # kept in the prompt per session
# the speech detector (speech.py): silero (default) or energy (loudness only)
VAD = env("RELAY_VAD", "silero").lower()
VAD_ON = env_float("RELAY_VAD_ON", 0.5)              # speech probability that starts speech
VAD_KEEP = env_float("RELAY_VAD_KEEP", 0.35)         # ...and that keeps it going
VAD_START_S = env_float("RELAY_VAD_START_S", 0.2)    # this much speech before it counts
VAD_MIN_SPEECH_S = env_float("RELAY_VAD_MIN_SPEECH_S", 0.25)  # less than this isn't a sentence
LOCK_DB = env_float("RELAY_LOCK_DB", 15.0)           # quieter than the speaker by this: background
SPEC_S = env_float("RELAY_SPEC_S", 0.3)              # quiet this long: start transcribing early

# the brain on Lite: think as little as the model allows (it's a chat, not a puzzle), and only
# let it search the web when the sentence sounds like it needs to (brain.needs_search)
os.environ.setdefault("REEDUS_GEMINI_THINK", "minimal")
LITE_SEARCH = env("RELAY_SEARCH", "auto").lower()    # auto | always | off

# money
LOW_BALANCE_USD = env_float("RELAY_LOW_BALANCE_USD", 1.0)
