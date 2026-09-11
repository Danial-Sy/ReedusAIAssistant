"""minimal local chat UI with the real voice, stdlib http server only."""
from __future__ import annotations
import argparse
import base64
import io
import json
import os
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from reedus_companion.serve.core import Reedus

# what reedus is doing right now, polled at /status while a turn is in flight so a 12s search
# turn shows "hang on" instead of a frozen "...". served on its own thread while /chat holds
# the lock; `seq` lets the page ignore stale reads.
STATUS = {"text": "", "seq": 0}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Reedus</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#101418; --panel:#1a2027; --me:#2b5278; --him:#243040; --text:#e6edf3; --dim:#8b98a5; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:16px/1.45 system-ui, sans-serif;
         display:flex; flex-direction:column; height:100vh; }
  header { padding:14px 20px; background:var(--panel); font-weight:600; letter-spacing:.3px;
           display:flex; align-items:center; gap:10px; }
  header .dot { width:10px; height:10px; border-radius:50%; background:#d4b106; }
  header .dot.ready { background:#3fb950; }
  #log { flex:1; overflow-y:auto; padding:20px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:75%; padding:10px 14px; border-radius:14px; white-space:pre-wrap; }
  .me  { align-self:flex-end; background:var(--me); border-bottom-right-radius:4px; }
  .him { align-self:flex-start; background:var(--him); border-bottom-left-radius:4px; }
  .him.thinking { color:var(--dim); font-style:italic; }
  form { display:flex; gap:10px; padding:14px 20px; background:var(--panel); }
  input { flex:1; padding:12px 14px; border-radius:10px; border:none; outline:none;
          background:#0d1117; color:var(--text); font-size:16px; }
  button { padding:12px 20px; border:none; border-radius:10px; background:var(--me);
           color:var(--text); font-size:16px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  #mic { background:#243040; min-width:54px; }
  #mic.rec { background:#a02c2c; animation:pulse 1.2s infinite; }
  #mic.off { display:none; }
  @keyframes pulse { 50% { filter:brightness(1.35); } }
  .hint { color:var(--dim); font-size:13px; text-align:center; padding:0 0 8px; }
  #stat { display:none; align-items:center; gap:12px; padding:10px 20px; background:#3a1215;
          color:#ff9b9b; font-size:14px; }
  #stat.on { display:flex; }
  #stat .reddot { width:10px; height:10px; border-radius:50%; background:#ff4d4d;
                  animation:pulse .9s infinite; flex:none; }
  #meter { flex:1; height:8px; background:#0d1117; border-radius:4px; overflow:hidden; }
  #lvl { height:100%; width:0%; background:linear-gradient(90deg,#3fb950,#d4b106,#ff4d4d);
         transition:width .06s linear; }
</style></head><body>
<header><span class="dot" id="dot"></span>Reedus</header>
<div id="log"></div>
<div class="hint">type, or talk: click the mic to start, click again to send (or hold Space outside the text box)</div>
<div id="stat"><span class="reddot"></span><span id="stattext">listening&hellip; click the mic again to send</span>
<div id="meter"><div id="lvl"></div></div></div>
<form id="f"><button id="mic" type="button" title="talk">&#127908;</button>
<input id="box" placeholder="say something..." autocomplete="off" autofocus>
<button id="send">send</button></form>
<script>
const log = document.getElementById("log"), box = document.getElementById("box"),
      form = document.getElementById("f"), send = document.getElementById("send"),
      mic = document.getElementById("mic"), dot = document.getElementById("dot");
fetch("/ping").then(r => r.json()).then(j => {
  dot.classList.add("ready");
  if (!j.mic) mic.classList.add("off");
});
function add(cls, text) {
  const d = document.createElement("div");
  d.className = "msg " + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
async function handleReply(think, j) {
  think.classList.remove("thinking"); think.textContent = j.reply;
  if (j.wav) new Audio("data:audio/wav;base64," + j.wav).play();
}
// While a turn is in flight the server publishes what it's DOING ("hang on, let me look that
// up"). A search turn takes ~12s; a bare "..." for that long reads as frozen, whereas naming
// the work reads as someone actually doing it.
function watchStatus(think) {
  let stop = false, last = -1;
  (async () => {
    while (!stop) {
      try {
        const s = await (await fetch("/status")).json();
        if (!stop && s.seq !== last && s.text) { last = s.seq; think.textContent = s.text; }
      } catch (e) { /* server busy generating, just try again */ }
      await new Promise(r => setTimeout(r, 400));
    }
  })();
  return () => { stop = true; };
}
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = box.value.trim(); if (!text) return;
  box.value = ""; send.disabled = true;
  add("me", text);
  const think = add("him thinking", "...");
  const unwatch = watchStatus(think);
  try {
    const r = await fetch("/chat", { method:"POST",
      headers:{ "Content-Type":"application/json" }, body: JSON.stringify({ text }) });
    const j = await r.json();
    unwatch();
    if (j.error) { think.textContent = "[server error: " + j.error + "]"; }
    else await handleReply(think, j);
  } catch (err) { unwatch(); think.textContent = "[error: " + err + ", is the server still running? check its terminal]"; }
  send.disabled = false; box.focus();
});

// ---- push-to-talk: click mic (or hold Space) -> record -> transcribe -> normal chat flow
const stat = document.getElementById("stat"), lvl = document.getElementById("lvl");
let rec = null, chunks = [], actx = null;

function micProblem() {
  if (!window.isSecureContext)
    return "mic is blocked on this address, browsers only allow it on localhost or https. " +
           "Open http://localhost:" + location.port + " on the machine itself " +
           "(an IP like http://192.168.x.x won't work over plain http).";
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    return "this browser doesn't support microphone capture (navigator.mediaDevices missing).";
  return null;
}

function meterLoop(stream) {
  actx = new (window.AudioContext || window.webkitAudioContext)();
  const an = actx.createAnalyser(); an.fftSize = 512;
  actx.createMediaStreamSource(stream).connect(an);
  const buf = new Uint8Array(an.frequencyBinCount);
  (function tick() {
    if (!rec) { lvl.style.width = "0%"; return; }
    an.getByteTimeDomainData(buf);
    let m = 0;
    for (let i = 0; i < buf.length; i++) m = Math.max(m, Math.abs(buf[i] - 128));
    lvl.style.width = Math.min(100, (m / 40) * 100) + "%";
    requestAnimationFrame(tick);
  })();
}

async function startRec() {
  if (rec) return;
  const prob = micProblem();
  if (prob) { add("him", "[" + prob + "]"); return; }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    add("him", "[mic error: " + err.name + ", " + (err.name === "NotAllowedError"
        ? "microphone permission is denied for this site. Click the icon in the address bar and allow it."
        : err.message) + "]");
    return;
  }
  chunks = [];
  rec = new MediaRecorder(stream);
  rec.ondataavailable = (e) => chunks.push(e.data);
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    if (actx) { actx.close(); actx = null; }
    const blob = new Blob(chunks, { type: rec.mimeType });
    rec = null; mic.classList.remove("rec"); stat.classList.remove("on");
    if (blob.size < 2000) { add("him", "[that was too short, hold the mic on a bit longer]"); return; }
    const me = add("me", "..."); me.classList.add("thinking");
    const think = add("him thinking", "...");
    try {
      const r = await fetch("/voice", { method:"POST",
        headers:{ "Content-Type": blob.type }, body: blob });
      const j = await r.json();
      me.classList.remove("thinking");
      me.textContent = j.you || "[didn't catch that, try again a little louder]";
      if (j.error) { think.textContent = "[server error: " + j.error + "]"; return; }
      if (j.reply) await handleReply(think, j); else think.remove();
    } catch (err) { me.remove(); think.textContent = "[error: " + err + "]"; }
  };
  rec.start();
  mic.classList.add("rec"); stat.classList.add("on");
  meterLoop(stream);
}
function stopRec() { if (rec && rec.state === "recording") rec.stop(); }
mic.addEventListener("click", () => rec ? stopRec() : startRec());
document.addEventListener("keydown", (e) => {
  if (e.code === "Space" && document.activeElement !== box && !e.repeat) { e.preventDefault(); startRec(); }
});
document.addEventListener("keyup", (e) => {
  if (e.code === "Space" && document.activeElement !== box) { e.preventDefault(); stopRec(); }
});
</script></body></html>"""


def wav_bytes(audio, sample_rate: int) -> bytes:
    import numpy as np
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def make_handler(reedus: Reedus, tts, lock: threading.Lock, stt_get=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/status":
                self._send(200, json.dumps(STATUS).encode())
            elif self.path == "/ping":
                self._send(200, json.dumps({"ok": True, "mic": stt_get is not None}).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def _reply_payload(self, text: str) -> dict:
            """LLM reply + spoken wav for some user text. Caller holds the lock."""
            STATUS["text"] = ""
            STATUS["seq"] += 1
            reply = reedus.reply(text)
            out = {"reply": reply}
            if tts is not None:
                try:
                    audio = tts.synth(reply)
                    if audio.size:
                        out["wav"] = base64.b64encode(
                            wav_bytes(audio, tts.sample_rate)).decode()
                except Exception as e:
                    out["tts_error"] = str(e)
            return out

        def do_POST(self):
            try:
                self._do_post()
            except Exception as e:
                import traceback
                traceback.print_exc()
                try:
                    self._send(200, json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
                except Exception:
                    pass

        def _do_post(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            if self.path == "/chat":
                text = json.loads(body or b"{}").get("text", "").strip()
                if not text:
                    self._send(400, b'{"error":"empty"}')
                    return
                with lock:  # one generation at a time
                    out = self._reply_payload(text)
                self._send(200, json.dumps(out).encode())
            elif self.path == "/voice" and stt_get is not None:
                suffix = ".ogg" if "ogg" in (self.headers.get("Content-Type") or "") else ".webm"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                    f.write(body)
                    tmp = f.name
                try:
                    with lock:
                        try:
                            text = stt_get().transcribe(tmp)
                        except Exception as e:
                            self._send(200, json.dumps(
                                {"you": "", "error": f"transcription failed: {e}"}).encode())
                            return
                        if not text.strip():
                            self._send(200, b'{"you":""}')
                            return
                        out = self._reply_payload(text)
                        out["you"] = text
                    self._send(200, json.dumps(out).encode())
                finally:
                    Path(tmp).unlink(missing_ok=True)
            else:
                self._send(404, b'{"error":"not found"}')

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["hf", "gguf"], default="gguf")
    ap.add_argument("--adapter", default=None, help="LoRA adapter dir (hf backend)")
    ap.add_argument("--merged", default=None, help="merged fp16 dir (hf backend)")
    ap.add_argument("--gguf", default=None, help="GGUF path (gguf backend)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-voice", action="store_true", help="text only, skip TTS")
    ap.add_argument("--no-mic", action="store_true", help="skip whisper / mic input")
    ap.add_argument("--cam", default=None,
                    help="camera source for vision: webcam index ('0') or phone URL "
                         "('http://admin:admin@192.168.1.235:8081'). Sets REEDUS_CAM.")
    ap.add_argument("--no-vision", action="store_true", help="disable the camera/vision path")
    args = ap.parse_args()

    if args.cam is not None:
        os.environ["REEDUS_CAM"] = args.cam
    if args.no_vision:
        os.environ["REEDUS_VISION"] = "0"

    if args.backend == "hf":
        from reedus_companion.serve.backend_hf import HFBackend
        backend = HFBackend(adapter_dir=args.adapter, merged_dir=args.merged)
    else:
        from reedus_companion.serve.backend_llamacpp import LlamaCppBackend
        backend = LlamaCppBackend(gguf_path=args.gguf)

    print(f"Loading Reedus ({args.backend})…")
    reedus = Reedus(backend)
    # prime the persona prefix at boot, otherwise the first message waits ~13s longer than every later one
    print("Warming…", end=" ", flush=True)
    print(f"{reedus.warm():.1f}s")

    def _publish_status(line: str) -> None:
        STATUS["text"] = line
        STATUS["seq"] += 1

    reedus.on_status = _publish_status

    tts = None
    if not args.no_voice:
        print("Loading voice…")
        from reedus_companion.voice.tts import TTS
        tts = TTS()

    stt_get = None
    if not args.no_mic:
        _stt_box, _stt_lock = {}, threading.Lock()

        def stt_get():
            with _stt_lock:
                if "stt" not in _stt_box:
                    print("Loading ears (whisper)…")
                    from reedus_companion.voice.stt import STT
                    _stt_box["stt"] = STT()
                return _stt_box["stt"]

        threading.Thread(target=stt_get, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", args.port),
                                 make_handler(reedus, tts, threading.Lock(), stt_get=stt_get))
    print(f"\nReedus is up, open http://localhost:{args.port}\n(Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        reedus.close()
        print("\nlater.")


if __name__ == "__main__":
    main()
