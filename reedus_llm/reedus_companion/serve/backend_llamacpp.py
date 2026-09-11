"""in-process llama-cpp-python backend. legacy path; backend_server.py is what runs on the xeon now."""
from __future__ import annotations
import contextlib
import io
import os
import time
from pathlib import Path
from typing import Callable, List, Dict, Optional

from reedus_companion import config


class _GgufDraft:
    """Neural draft using a small GGUF (the 1.7B). Experimental and slow on cpu, see config."""

    def __init__(self, llm, num_pred_tokens: int = 2):
        self.llm = llm
        self.num_pred_tokens = max(1, int(num_pred_tokens))
        self._prefix: list[int] = []

    def __call__(self, input_ids, /, **kwargs):  # noqa: ANN001
        import numpy as np
        ids = [int(x) for x in (input_ids.tolist() if hasattr(input_ids, "tolist") else input_ids)]
        n_ctx = int(getattr(self.llm, "n_ctx", lambda: 0)() or 0)
        if n_ctx and len(ids) + self.num_pred_tokens >= n_ctx:
            return np.array([], dtype=np.intc)
        try:
            shared = 0
            for a, b in zip(self._prefix, ids):
                if a != b:
                    break
                shared += 1
            if not self._prefix or shared < len(self._prefix):
                self.llm.reset()
                if ids:
                    self.llm.eval(ids)
                self._prefix = list(ids)
            elif len(ids) > shared:
                self.llm.eval(ids[shared:])
                self._prefix = list(ids)

            out: list[int] = []
            eos = self.llm.token_eos()
            for _ in range(self.num_pred_tokens):
                tok = int(self.llm.sample(temp=0.0))
                if tok == eos or tok < 0:
                    break
                out.append(tok)
                self.llm.eval([tok])
                self._prefix.append(tok)
            if out:
                self._prefix = self._prefix[: len(self._prefix) - len(out)]
                try:
                    self.llm.n_tokens = len(self._prefix)
                    self.llm._ctx.kv_cache_seq_rm(-1, len(self._prefix), -1)
                except Exception:
                    self._prefix = []
            return np.array(out, dtype=np.intc)
        except Exception:
            self._prefix = []
            try:
                self.llm.reset()
            except Exception:
                pass
            return np.array([], dtype=np.intc)


def _make_draft_model(mode: str, num_pred: int, draft_llm=None):
    """Build a draft_model for Llama(), or None."""
    mode = (mode or "off").lower().strip()
    if mode in ("0", "off", "false", "no", ""):
        return None
    if mode in ("draft", "neural", "gguf") and draft_llm is not None:
        return _GgufDraft(draft_llm, num_pred_tokens=num_pred)
    # lookup: ngram prompt lookup
    try:
        from llama_cpp.llama_speculative import LlamaPromptLookupDecoding
        return LlamaPromptLookupDecoding(max_ngram_size=2, num_pred_tokens=num_pred)
    except Exception as e:                                      # noqa: BLE001
        print(f"[reedus] speculative draft unavailable ({type(e).__name__}: {e})")
        return None


def _log_llama_cpu_caps() -> None:
    """Print CPU caps and whether the BLAS backend is actually linked."""
    raw = ""
    buf = io.StringIO()
    try:
        from llama_cpp import llama_print_system_info
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            got = llama_print_system_info()
        if isinstance(got, bytes):
            got = got.decode("utf-8", "replace")
        raw = f"{got or ''}\n{buf.getvalue()}"
    except Exception:
        raw = buf.getvalue()
    raw = raw.strip()
    if raw:
        one = " | ".join(line.strip() for line in raw.splitlines() if line.strip())
        print(f"[reedus] llama.cpp cpu: {one[:400]}")
    blas = _llama_blas_backend()
    if blas:
        print(f"[reedus] llama.cpp BLAS: {blas}")
    else:
        print("[reedus] WARNING: llama.cpp has no BLAS backend linked. "
              "CPU prefill will be slow. Rebuild on the Xeon with:\n"
              "  CMAKE_ARGS='-DGGML_NATIVE=ON -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS' "
              "FORCE_CMAKE=1 pip install --no-cache-dir --no-binary llama-cpp-python "
              "--force-reinstall --no-deps llama-cpp-python")


def _llama_blas_backend() -> str:
    """How BLAS is linked, or '' if it isn't."""
    try:
        import llama_cpp
        lib = Path(llama_cpp.__file__).resolve().parent / "lib" / "libggml-blas.so"
        if not lib.exists():
            lib = Path(llama_cpp.__file__).resolve().parent / "lib" / "libggml-blas.so.0"
        if not lib.exists():
            return ""
        import subprocess
        out = subprocess.check_output(["ldd", str(lib)], text=True, stderr=subprocess.DEVNULL)
        if "libopenblas" in out:
            return "OpenBLAS (libggml-blas.so)"
        if "libblas" in out or "libmkl" in out or "libblis" in out:
            return f"linked ({lib.name})"
        return "libggml-blas.so present"
    except Exception:
        return ""


def resolve_threads() -> int:
    """Physical cores, not SMT siblings."""
    if config.N_THREADS:
        return int(config.N_THREADS)
    env = os.environ.get("REEDUS_THREADS")
    if env and env.isdigit():
        return int(env)
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
        if n:
            return int(n)
    except Exception:
        pass
    try:                                    # linux: count distinct (physical id, core id)
        cores, phys, core = set(), None, None
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("physical id"):
                phys = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
                if phys is not None:
                    cores.add((phys, core))
        if cores:
            return len(cores)
    except Exception:
        pass
    return max(1, (os.cpu_count() or 4) // 2)


def _no_think(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Append Qwen3's "/no_think" soft switch to the system message."""
    if not messages:
        return messages
    head = messages[0]
    if head.get("role") != "system" or "/no_think" in (head.get("content") or ""):
        return messages
    return [{**head, "content": (head.get("content") or "") + "\n/no_think"}] + list(messages[1:])


class LlamaCppBackend:
    #: per-call timings, appended to only when REEDUS_PROFILE=1
    PROFILE: List[Dict[str, float]] = []

    def __init__(self, gguf_path: Optional[str] = None, n_ctx: int = config.N_CTX,
                 n_threads: Optional[int] = None, verbose: bool = False,
                 router_gguf: Optional[str] = None):
        gguf = gguf_path or str(config.GGUF_QUANT)
        if not Path(gguf).exists():
            raise FileNotFoundError(
                f"GGUF not found at {gguf}. Build it: deploy/export_gguf.sh")
        threads = int(n_threads) if n_threads else resolve_threads()
        self._verbose = verbose
        self.n_threads = threads
        self._spec_mode = (getattr(config, "SPECULATIVE", "off") or "off").lower().strip()
        self._spec_tokens = int(getattr(config, "SPEC_TOKENS", 2) or 2)
        self._draft_llm = None              # separate 1.7B, only when mode=draft
        self._want_spec = self._spec_mode not in ("0", "off", "false", "no", "")
        draft = None
        if self._want_spec and self._spec_mode in ("lookup", "prompt", "pld"):
            draft = _make_draft_model("lookup", self._spec_tokens)
        self.llm = self._build(gguf, n_ctx, threads, config.PROMPT_CACHE_MB, "main",
                               draft_model=draft)

        # router model (optional second instance). lazy on purpose: startup is already ~24s, so
        # warm() builds it when there's a warm phase and this only covers callers that skip warming.
        self._router_path = router_gguf if router_gguf is not None else config.ROUTER_GGUF
        self._router: Optional[object] = None      # None = unbuilt, False = unavailable
        if self._router_path and not Path(self._router_path).exists():
            print(f"[reedus] router model not found at {self._router_path}, "
                  f"running single-model")
            self._router_path = None
        if self._router_path:
            total = config.PROMPT_CACHE_MB + config.ROUTER_PROMPT_CACHE_MB
            print(f"[reedus] router split ON: {Path(self._router_path).name} "
                  f"threads={config.ROUTER_THREADS} n_ctx={config.ROUTER_N_CTX} "
                  f"cache={config.ROUTER_PROMPT_CACHE_MB}MB "
                  f"(+{config.PROMPT_CACHE_MB}MB main = {total}MB of prompt cache)")
            if total > 3072:
                print(f"[reedus] WARNING: {total}MB of prompt cache across two instances. "
                      f"On the 16 GB box drop REEDUS_PROMPT_CACHE_MB to 1024, the router "
                      f"benefits from cache more than the 4B does.")
            if config.ROUTER_THREADS + threads > (os.cpu_count() or 1):
                print(f"[reedus] WARNING: router({config.ROUTER_THREADS}) + "
                      f"main({threads}) threads exceeds the core count. llama.cpp does not "
                      f"coordinate threadpools; this can make the split slower.")
        if self._want_spec:
            print(f"[reedus] speculative decoding: mode={self._spec_mode} "
                  f"pred_tokens={self._spec_tokens}")
            # neural draft: load the 1.7B at main n_ctx and reuse it as the router so the box
            # doesn't hold two copies of the same gguf
            if self._spec_mode in ("draft", "neural", "gguf"):
                self._attach_neural_draft(n_ctx, threads)

    def router_ready(self) -> bool:
        """Is a second instance configured? Says nothing about whether it's built yet."""
        return bool(self._router_path)

    def _router_llm(self):
        """The small model, built on first use. False once it has failed so we don't retry every call."""
        if self._router is None:
            try:
                t0 = time.perf_counter()
                self._router = self._build(
                    self._router_path, config.ROUTER_N_CTX, config.ROUTER_THREADS,
                    config.ROUTER_PROMPT_CACHE_MB, "router")
                if self._verbose or os.environ.get("REEDUS_PROFILE"):
                    print(f"[reedus] router model loaded in "
                          f"{time.perf_counter() - t0:.1f}s")
            except Exception as e:                          # noqa: BLE001
                print(f"[reedus] router model failed to load ({type(e).__name__}: {e}), "
                      f"falling back to the main model for every call")
                self._router = False
        return None if self._router is False else self._router

    def _attach_neural_draft(self, n_ctx: int, threads: int) -> None:
        """Load a dedicated 1.7B at main n_ctx and wire it as draft_model."""
        path = self._router_path or config.ROUTER_GGUF
        if not path or not Path(path).exists():
            print("[reedus] speculative draft=gguf requested but no REEDUS_ROUTER_MODEL "
                  "- falling back to prompt-lookup")
            self.llm.draft_model = _make_draft_model("lookup", self._spec_tokens)
            return
        try:
            d_threads = max(2, min(config.ROUTER_THREADS, threads // 2 or 2))
            print(f"[reedus] loading speculative draft GGUF ({Path(path).name}, "
                  f"n_ctx={n_ctx}, threads={d_threads})…")
            t0 = time.perf_counter()
            cache_mb = min(config.ROUTER_PROMPT_CACHE_MB, 512)
            self._draft_llm = self._build(path, n_ctx, d_threads, cache_mb, "draft",
                                          draft_model=None, logits_all=False)
            self.llm.draft_model = _make_draft_model("draft", self._spec_tokens,
                                                    draft_llm=self._draft_llm)
            try:
                self.llm._logits_all = True
            except Exception:
                pass
            self._router = self._draft_llm
            print(f"[reedus] speculative 1.7B draft ready in {time.perf_counter() - t0:.1f}s "
                  f"(shared as router)")
        except Exception as e:                                  # noqa: BLE001
            print(f"[reedus] speculative draft GGUF failed ({type(e).__name__}: {e}), "
                  f"falling back to prompt-lookup")
            self._draft_llm = None
            self.llm.draft_model = _make_draft_model("lookup", self._spec_tokens)

    def _build(self, gguf: str, n_ctx: int, threads: int, cache_mb: int, label: str,
               draft_model=None, logits_all: Optional[bool] = None):
        from llama_cpp import Llama
        kw = dict(
            model_path=gguf,
            n_ctx=n_ctx,
            n_threads=threads,
            n_threads_batch=threads,
            n_batch=config.N_BATCH,
            n_ubatch=config.N_UBATCH,
            chat_format="qwen",
            verbose=self._verbose,
        )
        if draft_model is not None:
            kw["draft_model"] = draft_model
        if logits_all is True or (logits_all is None and draft_model is not None):
            kw["logits_all"] = True
        elif logits_all is False:
            kw["logits_all"] = False
        if label == "main" and self._want_spec and "logits_all" not in kw:
            kw["logits_all"] = True
        llm = Llama(**kw)
        kind = self._attach_cache(llm, cache_mb) if cache_mb else "none"
        if self._verbose or os.environ.get("REEDUS_PROFILE"):
            print(f"[reedus] llama.cpp[{label}]: threads={threads} n_batch={config.N_BATCH} "
                  f"n_ubatch={config.N_UBATCH} n_ctx={n_ctx} prompt_cache={kind}"
                  + (" speculative=on" if getattr(llm, "draft_model", None) else ""))
        if label == "main":
            self._cache_kind = kind
            self._cache_on = kind != "none"
            _log_llama_cpu_caps()
        return llm

    def _attach_cache(self, llm, cache_mb: int) -> str:
        """Attach a prompt cache to `llm`; returns the kind actually attached."""
        # warm turns 18.5s -> 4.8s with it. REEDUS_PROMPT_CACHE=0 off, _DISK=1 persists across restarts.
        kind = "none"
        if cache_mb > 0 and os.environ.get("REEDUS_PROMPT_CACHE", "1") != "0":
            # disk cache defaults off: it didn't spare the cold turn (saved states are sized by full
            # n_ctx so even 2 GB trims on every write) and warm() solves the same problem at startup.
            want_disk = os.environ.get("REEDUS_PROMPT_CACHE_DISK", "0") != "0"
            cap = cache_mb * 1024 * 1024
            if want_disk:
                try:
                    from llama_cpp import LlamaDiskCache
                    cache_dir = Path(os.environ.get(
                        "REEDUS_PROMPT_CACHE_DIR",
                        str(config.PROJECT_ROOT / "artifacts" / "prompt_cache")))
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    disk = LlamaDiskCache(cache_dir=str(cache_dir), capacity_bytes=cap)
                    _orig_setitem = type(disk).__setitem__

                    def _quiet_setitem(self, key, value, _f=_orig_setitem):
                        with contextlib.redirect_stdout(io.StringIO()), \
                                contextlib.redirect_stderr(io.StringIO()):
                            return _f(self, key, value)

                    disk.__class__ = type("QuietLlamaDiskCache", (LlamaDiskCache,),
                                          {"__setitem__": _quiet_setitem})
                    llm.set_cache(disk)
                    kind = f"disk:{cache_dir}"
                except Exception as e:
                    print(f"[reedus] disk prompt cache unavailable "
                          f"({type(e).__name__}: {e}), falling back to RAM")
            if kind == "none":
                try:
                    from llama_cpp import LlamaRAMCache
                    llm.set_cache(LlamaRAMCache(capacity_bytes=cap))
                    kind = "ram"
                except Exception as e:                  # old llama-cpp-python or OOM
                    print(f"[reedus] prompt cache unavailable ({type(e).__name__}: {e}), "
                          f"running without it")
        return kind

    # --- call routing ---
    def _pick(self, use_adapter: bool, tier: Optional[str], name: str):
        """(llama instance, tier label) for one call."""
        if tier == "main":
            return self.llm, "main"
        if tier != "router":
            if use_adapter or not self._router_path:
                return self.llm, "main"
            if name and name in config.ROUTER_KEEP_ON_MAIN:
                return self.llm, "main"
        if not self._router_path:
            return self.llm, "main"
        small = self._router_llm()
        if small is None:
            return self.llm, "main"
        return small, "router"

    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "",
             cancel=None) -> str:
        temp = config.TEMPERATURE if temperature is None else temperature
        llm, tier_label = self._pick(use_adapter, tier, name)
        if tier_label == "router" and config.ROUTER_NO_THINK:
            messages = _no_think(messages)
        t0 = time.perf_counter()

        do_stream = on_token is not None or cancel is not None
        if do_stream:
            pieces: List[str] = []
            n_completion = n_prompt = 0
            ttft = 0.0
            for chunk in llm.create_chat_completion(
                messages=messages, temperature=temp, top_p=config.TOP_P,
                repeat_penalty=config.REPEAT_PENALTY, max_tokens=max_tokens, stream=True,
            ):
                if cancel is not None and cancel.is_set():
                    break
                u = chunk.get("usage") or {}
                n_prompt = u.get("prompt_tokens", n_prompt)
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    if not ttft:
                        ttft = time.perf_counter() - t0
                    pieces.append(piece)
                    n_completion += 1
                    if on_token is not None:
                        try:
                            on_token(piece)
                        except Exception:
                            pass
                    if cancel is not None and cancel.is_set():
                        break
            if os.environ.get("REEDUS_PROFILE"):
                wall = time.perf_counter() - t0
                self.PROFILE.append({
                    "wall_s": wall,
                    "prompt_tokens": n_prompt,
                    "completion_tokens": n_completion,
                    "system_head": (messages[0].get("content", "")[:48] if messages else ""),
                    "tier": tier_label,
                    "name": name,
                    "ttft_s": ttft,
                    "gen_tok_s": (n_completion / (wall - ttft)) if wall > ttft else 0.0,
                    "cancelled": bool(cancel is not None and cancel.is_set()),
                })
            return "".join(pieces).strip()

        out = llm.create_chat_completion(
            messages=messages,
            temperature=temp,
            top_p=config.TOP_P,
            repeat_penalty=config.REPEAT_PENALTY,
            max_tokens=max_tokens,
        )
        if os.environ.get("REEDUS_PROFILE"):
            u = out.get("usage") or {}
            self.PROFILE.append({
                "wall_s": time.perf_counter() - t0,
                "prompt_tokens": u.get("prompt_tokens", 0),
                "completion_tokens": u.get("completion_tokens", 0),
                "system_head": (messages[0].get("content", "")[:48] if messages else ""),
                "tier": tier_label,
                "name": name,
            })
        return (out["choices"][0]["message"].get("content") or "").strip()
