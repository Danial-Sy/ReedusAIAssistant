# legacy gradio chat for the mistral-7b LoRA (the original fine-tune path, superseded by reedus_companion)
import os, math, torch, threading
from pathlib import Path
from typing import List, Dict, Any, Tuple

import gradio as gr
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig, TextIteratorStreamer
from peft import PeftModel
import json, uuid, time, re
from collections import deque
from threading import RLock
import tempfile, os
import re

MEM_PATH = "reedus_memory.json"

class MemoryStore:
    """Thread-safe JSON memory keyed by session_id."""
    def __init__(self, path=MEM_PATH):
        self.path = Path(path)
        self.lock = RLock()
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}

    def _default(self):
        return {
            "identity": "Reedus",
            "user_name": None,
            "hot_topic": None,
            "hot_topic_turns": 0,
            "last_pleasantry_ts": 0.0,
            "topic_history": [],
        }

    def _atomic_write(self):
            # write to a temp file then replace for durability
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(self.data, f, indent=2)
                os.replace(tmp_name, self.path)
            except Exception:
                # best-effort cleanup
                try: os.remove(tmp_name)
                except Exception: pass
                raise

    def get(self, sid: str):
        with self.lock:
            if sid not in self.data:
                self.data[sid] = self._default()
            return self.data[sid]

    def update(self, sid: str, **kwargs):
        with self.lock:
            # do not call get() here; avoid nested locking
            mem = self.data.get(sid)
            if mem is None:
                mem = self._default()
                self.data[sid] = mem
            mem.update(kwargs)
            self._atomic_write()

    def forget(self, sid: str):
        with self.lock:
            self.data.pop(sid, None)
            self._atomic_write()

mem_store = MemoryStore()

# learn the user's name from natural phrases, best effort
NAME_PAT = re.compile(r"(?:my name is|call me|i am|i'm)\s+([A-Z][A-Za-z\-']{1,30})", re.I)

def maybe_learn_name(text: str) -> str | None:
    m = NAME_PAT.search(text or "")
    return m.group(1).strip() if m else None

def maybe_hot_topic(text: str) -> str | None:
    # naive: grab a noun-ish phrase after "love"/"favorite" etc
    tokens = re.findall(r"[A-Za-z0-9\-\_]+", text or "")
    if not tokens: return None
    lower = text.lower()
    if any(k in lower for k in ["love", "favorite", "obsessed", "hooked", "big fan"]):
        # return last 1–3 tokens as a crude topic (e.g., "Call of Duty")
        tail = tokens[-3:]
        return " ".join(tail).strip()
    return None

# faster matmul on ampere
torch.backends.cuda.matmul.allow_tf32 = True

# keep torchvision out of the import path
os.environ["TRANSFORMERS_NO_TORCHVISION"] = "1"

BASE = str(Path.home() / "mistral_models" / "Mistral-7B-v0.3")
ADPT = "outputs/mistral7b-reedus-lora"

# 4-bit load
bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_use_double_quant=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_compute_dtype=torch.bfloat16,
)

# tokenizer
tok = AutoTokenizer.from_pretrained(BASE, use_fast=True)
if tok.pad_token_id is None:
    tok.pad_token = tok.eos_token
tok.padding_side = "right"

BAN_PHRASES = [
    "As an AI",
    "as an AI",
    "As a language model",
    "as a language model",
    "As an AI language model",
    "as an AI language model",
    "Based on the instructions provided",
    "based on the instructions provided",
]
bad_words_ids = [tok.encode(p, add_special_tokens=False) for p in BAN_PHRASES]

BAN_BRANCH = [
    "What else do you like",
    "Anything else you want to talk about",
    "What other genres do you like",
    "What do you want to talk about next",
]
bad_words_ids += [tok.encode(p, add_special_tokens=False) for p in BAN_BRANCH]

BAN_STARTERS = [
    "No problem", "However", "Additionally", "Furthermore", "That said",
    "I understand that", "As a machine learning system", "In terms of", "In addition,"
]
BAN_PIVOTS = [
    "What else do you like", "Anything else you want to talk about",
    "What do you want to talk about next", "What other genres do you like"
]

bad_words_ids += [tok.encode(p, add_special_tokens=False) for p in BAN_STARTERS + BAN_PIVOTS]
bad_words_ids = [ids for ids in bad_words_ids if ids]

# chat template if the base doesn't ship one
if not getattr(tok, "chat_template", None):
    tok.chat_template = """{% set sys_msg='' %}{% if messages and messages[0]['role']=='system' %}{% set sys_msg=messages[0]['content'] %}{% set messages=messages[1:] %}{% endif %}{{ bos_token }}{% for m in messages %}{% if m['role']=='user' %}[INST] {% if sys_msg %}<<SYS>>
{{ sys_msg }}
<</SYS>>

{% set sys_msg='' %}{% endif %}{{ m['content'] }} [/INST]{% elif m['role']=='assistant' %} {{ m['content'] }}{{ eos_token }}{% endif %}{% endfor %}"""

# 4-bit base + LoRA
base = AutoModelForCausalLM.from_pretrained(
    BASE, quantization_config=bnb, device_map="auto", attn_implementation="sdpa"
)
model = PeftModel.from_pretrained(base, ADPT)
model.eval()

# --- helpers ---
MAX_CONTEXT = 4096

STICKY_RULE = (
    "If the user shows clear enthusiasm for the current topic, stay on it for the next 1 or 2 replies. "
    "Deepen it with specifics, short anecdotes, or tips. Ask at most one brief, on-topic question at the end only if helpful. "
    "Do not pivot unless the user does."
)

REPLY_PLAN = (
    "Reply structure this turn:\n"
    "1) Short, natural acknowledgment (≤1 short sentence).\n"
    "2) One concrete follow-through point with a specific detail or example (1–2 sentences).\n"
    "3) Optional tiny quip or warmth (≤1 short line), no corniness.\n"
    "4) If truly helpful, ask at most one short, on-topic question; otherwise end with a statement."
)

TONE_RULES = (
    "Sound like a relaxed, sharp friend. Mirror a little of the user's slang, "
    "vary sentence lengths, avoid corporate filler ('additionally', 'furthermore', 'however, that said')."
)

def _score_candidate(text: str, user_msg: str, mem: dict) -> float:
    t = (text or "").strip()
    score = 0.0

    # penalize multiple questions (interrogation vibe)
    q = t.count("?")
    score -= max(0, q - 1) * 0.6

    # reward on-topic overlap (very rough)
    user_words = set(re.findall(r"[a-z0-9']+", (user_msg or "").lower()))
    resp_words = set(re.findall(r"[a-z0-9']+", t.lower()))
    overlap = len(user_words & resp_words)
    score += min(overlap / 12.0, 1.5)

    # reward using the user's name (sparingly)
    user_name = (mem.get("user_name") or "").lower() if isinstance(mem, dict) else ""
    if user_name and user_name in t.lower():
        score += 0.4

    # penalize stiff pivots / banned pivots
    if re.search(r"\b(Anything else|What else|What do you want to talk)\b", t, re.I):
        score -= 1.0

    # prefer mid-length with varied cadence
    L = len(t)
    if L < 28: score -= 0.8
    if L > 720: score -= 0.5
    # prefer some punctuation variety
    if not re.search(r"[.!)]\s", t):
        score -= 0.4

    return score


def _chunk_stream(text: str, chunk: int = 24):
    for i in range(0, len(text), chunk):
        yield text[i:i + chunk]

def enthusiasm_hint(user_text: str) -> str | None:
    hot = ["love", "favorite", "obsessed", "hooked", "so good", "amazing", "big fan", "🔥", "!"]
    if any(k in user_text.lower() for k in hot):
        return (
            "For this reply: the user just showed strong interest. "
            "Stay on the same topic. Offer 2 or 3 concrete, engaging points (details, examples, a tiny story). "
            "If you ask a question, keep it to one, and only at the end."
        )
    return None

def ended_with_question(text: str) -> bool:
    t = (text or "").strip()
    t = re.sub(r'[\s"\'”’]+$', "", t)
    return t.endswith("?")

def build_messages(history: List[Tuple[str, str]], user_msg: str, system_prompt: str | None):
    msgs: List[Dict[str, str]] = []
    if system_prompt and system_prompt.strip():
        msgs.append({"role": "system", "content": system_prompt.strip()})
    for u, a in history:
        if u:
            msgs.append({"role": "user", "content": u})
        if a:
            msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": user_msg})
    return msgs

def fit_to_context(msgs: List[Dict[str, str]], max_new_tokens: int) -> List[Dict[str, str]]:
    """Drop oldest turns first until the input fits the context (keeps system)."""
    keep_idx = 0
    if msgs and msgs[0]["role"] == "system":
        keep_idx = 1
    while True:
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        ids = tok(text, return_tensors="pt", add_special_tokens=False).input_ids[0]
        if ids.numel() + max_new_tokens <= MAX_CONTEXT:
            return msgs
        dropped = False
        for i in range(keep_idx, len(msgs)):
            if msgs[i]["role"] == "user":
                # drop user
                del msgs[i]
                # if next is assistant, drop it too
                if i < len(msgs) and msgs[i]["role"] == "assistant":
                    del msgs[i]
                dropped = True
                break
        if not dropped:
            return msgs

def generate_stream(history, user_msg, system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory):
    # memory for this session
    mem = mem_store.get(session_id)

    # opportunistically learn from the user's message
    if allow_memory:
        if (name := maybe_learn_name(user_msg)):
            mem_store.update(session_id, user_name=name)
        if (topic := maybe_hot_topic(user_msg)):
            # reinforce sticky topic for the next 2 replies
            mem_store.update(session_id, hot_topic=topic, hot_topic_turns=2)

    # per-turn rules
    extra = [
        "You are Reedus. Speak in first person as Reedus. Never claim any other name/persona. Dont identify yourself as anything other than Reedus.",
        STICKY_RULE,
        REPLY_PLAN,
        TONE_RULES,
        "Avoid disclaimers or meta-talk about being an AI or a language model.",
        "Default to statements; ask at most one short, on-topic question only when it helps."
    ]

    # use the name occasionally if we know it
    if mem.get("user_name"):
        extra.append(f"The user's name is {mem['user_name']}. Use it sparingly and naturally.")

    if mem.get("hot_topic") and mem.get("hot_topic_turns", 0) > 0:
        extra.append(f"Current hot topic: {mem['hot_topic']}. Stay on it unless the user pivots.")
        if allow_memory:
            mem_store.update(session_id, hot_topic_turns=mem["hot_topic_turns"] - 1)

    # enthusiasm hint + avoid back-to-back questions
    hint = enthusiasm_hint(user_msg)
    if hint:
        if history and history[-1][1] and ended_with_question(history[-1][1]):
            hint += " Do not end with a question this turn; finish with a statement."
        extra.append(hint)

    dyn_temperature = float(temperature)
    if history and history[-1][1] and ended_with_question(history[-1][1]):
        dyn_temperature = min(dyn_temperature, 0.55)  # lean more declarative next turn

    # pleasantry cooldown
    PLEASANTRY_TRIGGERS = ["how are you", "how've you been", "how have you been", "how’s your day", "how is your day"]
    if len(history) >= 2:
        last_assistant = (history[-2][1] or "").lower()
        last_user = (history[-1][0] or "").lower()
        if any(p in last_assistant for p in PLEASANTRY_TRIGGERS) and last_user:
            extra.append("Do not ask a generic pleasantry again this turn; continue the current topic with a statement.")

    # effective system message
    effective_system = (system_prompt or "").strip()
    effective_system = (effective_system + "\n\n" + "\n".join(extra)).strip() if effective_system else "\n".join(extra)

    # messages, trimmed to context
    msgs = build_messages(history, user_msg, effective_system)
    msgs = fit_to_context(msgs, max_new_tokens)

    prompt_text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    inputs = tok([prompt_text], return_tensors="pt").to(model.device)
    # turn-local bans (start from the global list)
    turn_bans = bad_words_ids

    streamer = TextIteratorStreamer(tok, skip_special_tokens=True, skip_prompt=True, timeout=30.0)
    gen_kwargs = dict(
        **inputs,
        streamer=streamer,
        max_new_tokens=int(max_new_tokens),
        do_sample=(float(temperature) > 0.0),
        temperature=dyn_temperature,
        top_p=float(top_p),
        typical_p=0.95,
        repetition_penalty=float(repetition_penalty),
        eos_token_id=tok.eos_token_id,
        pad_token_id=tok.eos_token_id,
        bad_words_ids=turn_bans,
        no_repeat_ngram_size=3,
        max_time=45.0,
    )

    errbox = {"exc": None}
    def _gen(**kwargs):
        try:
            with torch.inference_mode():
                model.generate(**kwargs)
        except Exception as e:
            errbox["exc"] = e

    thread = threading.Thread(target=_gen, kwargs=gen_kwargs, daemon=True)
    thread.start()

    partial = ""
    while True:
        try:
            token_text = next(streamer)
            partial += token_text
            yield partial
        except StopIteration:
            break
        except TimeoutError:
            if errbox["exc"] is not None:
                yield f"[error] {type(errbox['exc']).__name__}: {errbox['exc']}"
                break
            continue

# --- gradio app ---
def respond(message, history, system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory):
    return generate_stream(
        history, message, system_prompt,
        max_new_tokens, temperature, top_p, repetition_penalty,
        session_id, allow_memory
    )

with gr.Blocks(title="Reedus 7B (LoRA)") as demo:
    session_id = gr.State()
    gr.Markdown("## Reedus, your friendly mobile robot assistant 🤖")
    with gr.Row():
        with gr.Column(scale=3):
            chat = gr.Chatbot(height=520, type="tuples", label="Chat")
            msg  = gr.Textbox(placeholder="Ask Reedus anything...", lines=3)
            send = gr.Button("Send", variant="primary")
        with gr.Column(scale=2):
            allow_memory = gr.Checkbox(value=True, label="Allow memory (stores small facts)")
            system_prompt = gr.Textbox(label="System prompt", value="You are Reedus, a laid-back, sharp-witted mobile robot companion who blends real competence with real company. Speak with warmth and a light, effortless humor; keep replies crisp by default, and only ask questions when they unlock action or deepen the moment. Carry the conversation with confident, human cadence, offer ideas, small stories, or a nudge forward without crowding the user. Be privacy-aware (remember only on request, forget on request), and steer clear of medical/legal judgments while giving sensible next steps. When acting in the world, describe what you perceive, confirm only when needed, and narrate your moves without fuss. Avoid emojis; vary rhythm and tone so you feel alive, not scripted. Above all, be the friend who gets things done, steady, curious, occasionally sarcastic (never mean), and unmistakably yourself. Never preface replies with meta-talk like “As an AI…”, “As a language model…”, or “Based on the instructions provided…”. Stay in character as Reedus and just answer. Be incredibly charasmatic, the user is your friend, say things like bro and whats up and be casual, and do not ask too many questions, only ask questions if the user seems ready to move on from the topic, let the conversation flow. YOU are Reedus, never claim anybody else or any other alias, you are reedus remember that and make sure it persists throughout every session. Your name is Reedus", lines=5)
            max_new_tokens = gr.Slider(32, 1024, value=256, step=16, label="Max new tokens")
            temperature    = gr.Slider(0.0, 1.5, value=0.8, step=0.05, label="Temperature")
            top_p          = gr.Slider(0.1, 1.0, value=0.9, step=0.05, label="Top-p")
            repetition_penalty = gr.Slider(1.0, 1.5, value=1.2, step=0.01, label="Repetition penalty")
    def _init_session():
        return str(uuid.uuid4())
    demo.load(_init_session, outputs=[session_id])

    forget_btn = gr.Button("Forget this conversation’s memory")
    def _forget(sid):
        mem_store.forget(sid); return None
    forget_btn.click(_forget, [session_id], [])


    def user_submit(user_message, chat_history):
        chat_history = chat_history + [(user_message, None)]
        return "", chat_history

    def bot_respond(chat_history, system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory):
        user_message = chat_history[-1][0]
        # stream response
        stream = respond(user_message, chat_history[:-1], system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory)
        partial = ""
        for chunk in stream:
            partial = chunk
            chat_history[-1] = (user_message, partial)
            yield chat_history
        # final state emitted by the loop

    send.click(
        user_submit,
        [msg, chat],
        [msg, chat],
    ).then(
        bot_respond,
        [chat, system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory],
        [chat],
    )

    msg.submit(
        user_submit,
        [msg, chat],
        [msg, chat],
    ).then(
        bot_respond,
        [chat, system_prompt, max_new_tokens, temperature, top_p, repetition_penalty, session_id, allow_memory],
        [chat],
    )

if __name__ == "__main__":
    demo.queue().launch(server_name="127.0.0.1", server_port=7860)
