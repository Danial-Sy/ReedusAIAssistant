"""chat_bridge, how ReedOS reaches Reedus's brain."""

import asyncio
import importlib
import json
import logging
import urllib.request

log = logging.getLogger("chat")


class ChatBridge:
    def __init__(self, settings: dict, link=None):
        self.settings = settings
        self.link = link
        self.brain = None
        self.chat_url = None
        self.configure(settings)

    def configure(self, settings: dict):
        self.settings = settings
        self.chat_url = settings.get("_chat_url")
        if self.link is not None:
            self.link.configure(settings)
        if self.brain is None:
            try:
                self.brain = importlib.import_module("reedus_brain")
                log.info("reedus_brain loaded, model is in-process")
            except ImportError:
                self.brain = None

    @property
    def linked(self) -> bool:
        """True when the running Reedus is reachable. Cheap, health is cached."""
        return self.link is not None and self.link.online

    @property
    def available(self) -> bool:
        return self.linked or self.brain is not None or self.chat_url is not None

    async def ask(self, prompt: str, speak: bool = False) -> dict:
        """One turn. `speak` routes the reply out of the box speakers in his real voice."""
        if not self.settings.get("llmEnabled", True):
            return {"text": "(LLM brain is switched off in robot settings)", "tts": False}

        if self.linked:
            out = await asyncio.to_thread(self.link.ask, prompt, speak)
            if out.get("error"):
                log.warning("brain link failed: %s", out["error"])
                if not (self.brain or self.chat_url):
                    return {"text": f"(Reedus is up but wouldn't answer: {out['error']})",
                            "tts": False}
            else:
                return {
                    "text": out.get("text", "…"),
                    "tts": not out.get("spoke"),
                    "spoke": bool(out.get("spoke")),
                    "mode": out.get("mode"),
                    "ms": out.get("ms"),
                }

        if self.brain is not None:
            try:
                text = await asyncio.to_thread(self.brain.generate, prompt, self.settings)
                return {"text": text, "tts": True}
            except Exception as e:
                log.exception("reedus_brain.generate failed")
                return {"text": f"(brain error: {e})", "tts": False}

        if self.chat_url:
            try:
                text = await asyncio.to_thread(self._http_ask, prompt)
                return {"text": text, "tts": True}
            except Exception as e:
                log.warning("chat endpoint failed: %s", e)
                return {"text": "(model endpoint unreachable)", "tts": False}

        return {"text": "My brain isn't running, start the voice loop "
                        "(or reedus_companion.serve.control_api) and I'll wake right up.",
                "tts": False}

    def _http_ask(self, prompt: str) -> str:
        req = urllib.request.Request(
            self.chat_url,
            data=json.dumps({"prompt": prompt, "settings": self.settings}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("text", "…")

    async def speak(self, text: str):
        """Say something in his real voice, the routine `say` block lands here."""
        if self.linked:
            out = await asyncio.to_thread(self.link.say, text)
            if not out.get("error"):
                return
            log.warning("brain speak failed: %s", out["error"])
        if self.brain is not None and hasattr(self.brain, "speak"):
            try:
                await asyncio.to_thread(self.brain.speak, text, self.settings)
            except Exception:
                log.exception("speak failed")

    async def play_sound(self, name: str):
        if not self.settings.get("robotNoises", True):
            return
        if self.brain is not None and hasattr(self.brain, "play_sound"):
            try:
                await asyncio.to_thread(self.brain.play_sound, name, self.settings)
            except Exception:
                log.exception("play_sound failed")
