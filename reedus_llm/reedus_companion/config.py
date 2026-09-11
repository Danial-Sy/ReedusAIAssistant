"""central config. most knobs are REEDUS_* env vars so the xeon.env file can pin them."""
import os
from pathlib import Path

# --- paths ---
PROJECT_ROOT = Path(__file__).resolve().parent
REEDUS_LLM_ROOT = PROJECT_ROOT.parent

NORMALIZED_DIR = REEDUS_LLM_ROOT / "data" / "normalized"

BLEND_DIR = PROJECT_ROOT / "artifacts" / "blended"
TRAIN_PATH = BLEND_DIR / "reedus_train"
VAL_PATH = BLEND_DIR / "reedus_val"
OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "reedus-lora"
MERGED_DIR = PROJECT_ROOT / "artifacts" / "reedus-merged-fp16"
GGUF_DIR = PROJECT_ROOT / "artifacts" / "gguf"
# Q4_K_M is 2.4 GB and ~15% faster prefill on the E5 than Q5_K_M for basically the same quality
GGUF_QUANT_TYPE = os.environ.get("REEDUS_GGUF_QUANT", "Q4_K_M")
GGUF_QUANT = GGUF_DIR / f"reedus-{GGUF_QUANT_TYPE}.gguf"

MEMORY_DIR = PROJECT_ROOT / "memory"
BULLETIN_PATH = MEMORY_DIR / "bulletin.md"
EPISODIC_DB = MEMORY_DIR / "episodic.sqlite"
TOPICS_PATH = MEMORY_DIR / "topics.json"
# sharded corpus (see knowledge/). REEDUS_KNOWLEDGE_DIR moves just the corpus, it's the only
# thing doing big random reads per turn so on the xeon it lives on the nvme.
KNOWLEDGE_DIR = Path(os.environ.get("REEDUS_KNOWLEDGE_DIR", str(MEMORY_DIR / "knowledge")))
# resident shards. a shard is just an mmap + a 3 MB offset index now, and a turn can probe
# 6 of them, so anything under ~8 thrashes. 12 covers the widest turn plus the widen-on-miss scan.
KNOWLEDGE_MAX_HOT = int(os.environ.get("REEDUS_KNOWLEDGE_MAX_HOT", "12"))

MODELS_DIR = PROJECT_ROOT / "models"
VOICE_DIR = MODELS_DIR / "voice"

# hand-authored personality data, oversampled at train time
VOICE_SEEDS_JSONL = PROJECT_ROOT / "data" / "reedus_voice.jsonl"
IDENTITY_SEEDS_JSONL = PROJECT_ROOT / "data" / "reedus_identity.jsonl"
TOOLS_SEEDS_JSONL = PROJECT_ROOT / "data" / "reedus_tools.jsonl"
CONVO_SEEDS_JSONL = PROJECT_ROOT / "data" / "reedus_convo.jsonl"

# --- LLM ---
BASE_MODEL = "Qwen/Qwen3-4B-Instruct-2507"   # apache-2.0, non-thinking, native tool-calling

MAX_LEN = 2048

LORA = dict(
    r=32, lora_alpha=64, lora_dropout=0.05, bias="none", task_type="CAUSAL_LM",
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
)

TRAIN_ARGS = dict(
    num_train_epochs=2, learning_rate=2e-4, lr_scheduler_type="cosine",
    warmup_ratio=0.03, weight_decay=0.0, max_grad_norm=0.3,
    per_device_train_batch_size=2, gradient_accumulation_steps=8,
    per_device_eval_batch_size=2, eval_steps=500, save_steps=500, logging_steps=20,
    save_total_limit=3, gradient_checkpointing=True, optim="paged_adamw_8bit",
)
_PRECISION = os.environ.get("REEDUS_TRAIN_PRECISION", "bf16").lower()
TRAIN_ARGS["bf16"] = _PRECISION == "bf16"
TRAIN_ARGS["fp16"] = _PRECISION == "fp16"
EARLY_STOPPING_PATIENCE = 3

CONVO_REPEAT = int(os.environ.get("REEDUS_CONVO_REPEAT", "20"))
VOICE_SEED_REPEAT = int(os.environ.get("REEDUS_VOICE_REPEAT", "5"))
IDENTITY_REPEAT = 40
TOOLS_REPEAT = 30

# --- serving ---
N_CTX = int(os.environ.get("REEDUS_N_CTX", "4096"))
N_THREADS = None
N_BATCH = int(os.environ.get("REEDUS_N_BATCH", "512"))
N_UBATCH = int(os.environ.get("REEDUS_N_UBATCH", "512"))
# multi-slot prompt cache. a turn runs several calls with different system prompts and
# llama-cpp-python only keeps the last prefix, so without this each one re-prefills 1.3-2k
# tokens (warm chitchat 18.5s -> 4.8s with it). 1024 MB evicts between turns, 2048 holds all
# five shapes. first thing to shrink if the xeon runs out of RAM.
PROMPT_CACHE_MB = int(os.environ.get("REEDUS_PROMPT_CACHE_MB", "2048"))

# --- router model (the split) ---
# most of a turn is spent in calls that emit 10-30 tokens of JSON (5.3s of a 14.4s turn), so
# those run on a separate small model (qwen3-1.7b) when REEDUS_ROUTER_MODEL is set.
ROUTER_GGUF = os.environ.get("REEDUS_ROUTER_MODEL", "") or None

# qwen3 honours "/no_think" in the system prompt; appended to router-tier calls only
ROUTER_NO_THINK = os.environ.get("REEDUS_ROUTER_NO_THINK", "1") == "1"

# two llama.cpp pools of 14 threads on a 14-core box are slower than one, so the router gets a
# small budget. swept on the build box: 2 -> 1.05s, 4 -> 0.70s, 6 -> 0.62s. re-sweep on the xeon.
ROUTER_THREADS = int(os.environ.get("REEDUS_ROUTER_THREADS", "4"))

# router prompts are 180-600 tokens + 6 history messages. saved llama states are sized by
# n_ctx so this is the real lever on cache size. don't go below ~1280, overflow truncates silently.
ROUTER_N_CTX = int(os.environ.get("REEDUS_ROUTER_N_CTX", "1536"))

# speculative decoding via llama-cpp-python draft_model. measured on the xeon: ~0.8 tok/s and
# ~18s prefill vs ~4 tok/s / ~3s baseline, because logits_all=True alone hurts. keep it off on cpu.
SPECULATIVE = os.environ.get("REEDUS_SPECULATIVE", "0").strip().lower()
SPEC_TOKENS = int(os.environ.get("REEDUS_SPEC_TOKENS", "2"))

# adds to PROMPT_CACHE_MB, doesn't share it. don't fund it by shrinking the main cache: at
# main=1024/router=2048 the persona re-prefilled every turn. big states get the big cache.
ROUTER_PROMPT_CACHE_MB = int(os.environ.get("REEDUS_ROUTER_PROMPT_CACHE_MB", "768"))

# calls that stay on the main model even with a router model configured, because the 1.7B
# measurably wrecks them (eval_runs/split_*.txt): topic-brief gets STORED in topics.json so a
# bad one is a persistent bad fact, subjects silence precision fell 88% -> 50%, correction
# precision 93% -> 74% and it feeds DPO data, look-router fails closed so a bad parse means the
# camera never opens. extract rides the 1.7B; move it back here if numbers come back mangled.
ROUTER_KEEP_ON_MAIN = tuple(
    n.strip() for n in os.environ.get(
        "REEDUS_ROUTER_KEEP_ON_MAIN",
        "topic-brief,subjects,correction,look-router").split(",") if n.strip())

MAX_NEW_TOKENS = 512
TEMPERATURE = 0.65
TOP_P = 0.9
REPEAT_PENALTY = 1.15

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
RECALL_TOP_K = 4
RECALL_MIN_SCORE = 0.45     # below this recall drags unrelated old convos (and their hallucinated facts) back in

SEARCH_PROVIDER = "auto"
SEARCH_MAX_RESULTS = 5

# --- voice ---
STT_MODEL = os.environ.get("REEDUS_STT_MODEL", "small")   # tiny|base|small on cpu, large-v3 on gpu
STT_DEVICE = os.environ.get("REEDUS_STT_DEVICE", "auto")
STT_COMPUTE = os.environ.get("REEDUS_STT_COMPUTE", "int8")  # int8 cpu, float16 gpu
STT_LANGUAGE = "en"
STT_INITIAL_PROMPT = os.environ.get("REEDUS_STT_PROMPT", "Reedus.")
STT_HOTWORDS = os.environ.get("REEDUS_STT_HOTWORDS", "Reedus")

def _audio_device(var: str, kind: str = "output"):
    raw = os.environ.get(var, "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    try:
        import sounddevice as sd
        needle = raw.lower()
        hits = []
        for i, dev in enumerate(sd.query_devices()):
            name = (dev.get("name") or "")
            if needle not in name.lower():
                continue
            if kind == "input" and int(dev.get("max_input_channels") or 0) <= 0:
                continue
            if kind == "output" and int(dev.get("max_output_channels") or 0) <= 0:
                continue
            hits.append((i, name))
        if not hits:
            return raw
        if len(hits) == 1:
            return hits[0][0]
        if kind == "output":
            for i, name in hits:
                low = name.lower()
                if "pulse" in low or "pipewire" in low:
                    return i
            for i, name in hits:
                if "hw:" not in name.lower():
                    return i
            return hits[0][0]
        for i, name in hits:
            if "hw:" in name.lower() and "pulse" not in name.lower() and "pipewire" not in name.lower():
                return i
        return hits[0][0]
    except Exception:
        return raw


TTS_DEVICE = _audio_device("REEDUS_TTS_DEVICE", "output")
MIC_DEVICE = _audio_device("REEDUS_MIC_DEVICE", "input")

from reedus_companion.voice import voice_prefs as _voice_prefs   # noqa: E402
KOKORO_LANG = _voice_prefs.kokoro_lang()
KOKORO_VOICE = _voice_prefs.kokoro_fallback_voice()
KOKORO_SPEED = float(os.environ.get("REEDUS_SPEED", "1.10"))
TTS_SAMPLE_RATE = 24000     # kokoro is 24 kHz

KOKORO_VOICE_BLEND = _voice_prefs.kokoro_blend()

VOICE_PITCH_SEMITONES = float(os.environ.get("REEDUS_PITCH", "1.0"))
VOICE_PITCH_RANGE = float(os.environ.get("REEDUS_PITCH_RANGE", "1.4"))

TTS_SPEED_EXCLAIM = 1.15
TTS_SPEED_ELLIPSIS = 0.90

TTS_PITCH_QUESTION = float(os.environ.get("REEDUS_PITCH_QUESTION", "0.7"))
TTS_PITCH_EXCLAIM = float(os.environ.get("REEDUS_PITCH_EXCLAIM", "1.0"))
TTS_PITCH_ELLIPSIS = float(os.environ.get("REEDUS_PITCH_ELLIPSIS", "-0.6"))

TTS_TRIM_DB = 35.0

ROBOT_AMOUNT = float(os.environ.get("REEDUS_ROBOT", "0.0"))
ROBOT_CHORUS_HZ = 0.7
ROBOT_BITS = 7.0

VOICE_PRONUNCIATIONS = {
    "Reedus": "[Reedus](/ˈɹiːdəs/)",
}

# pacing: replies are synthesized per segment (split at sentence enders and "..."), kokoro's own
# silence padding is trimmed, then these gaps go in (jittered ~20% so it isn't metronomic).
# REEDUS_PAUSE scales them all, 0 = one connected pass.
PAUSE_SCALE = float(os.environ.get("REEDUS_PAUSE", "1"))
TTS_PAUSE_SENTENCE = 0.21 * PAUSE_SCALE    # after .
TTS_PAUSE_QUESTION = 0.25 * PAUSE_SCALE    # after ?
TTS_PAUSE_EXCLAIM = 0.18 * PAUSE_SCALE     # after !
TTS_PAUSE_ELLIPSIS = 0.30 * PAUSE_SCALE    # after ...
TTS_PAUSE_SOFT = 0.05 * PAUSE_SCALE        # after an em dash

# mic capture
MIC_SAMPLE_RATE = 16000
# trailing silence before end of utterance. 0.85s got tripped by soft consonants and cut mid-sentence
MIC_SILENCE_SEC = float(os.environ.get("REEDUS_MIC_SILENCE_SEC", "1.35"))
MIC_MAX_SEC = float(os.environ.get("REEDUS_MIC_MAX_SEC", "45"))
MIC_RMS = float(os.environ.get("REEDUS_MIC_RMS", "0.012"))
MIC_HANGOVER = float(os.environ.get("REEDUS_MIC_HANGOVER", "0.28"))
