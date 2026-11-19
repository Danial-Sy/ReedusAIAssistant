"""what every non-Gemini cloud backend shares: the Backend protocol core.py calls, the budget
breaker, retries on transient errors, plain-English errors, and per-call profiling.

a subclass implements one method, _once(), and returns (text, tokens_in, tokens_out, searches).
"""
from __future__ import annotations

import os
import re
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from reedus_companion.cloud import CloudUnavailable, budget, persona_shots, providers

_TRANSIENT_NAMES = {
    "APIConnectionError", "APITimeoutError", "InternalServerError", "ServiceUnavailableError",
    "OverloadedError", "ConnectError", "ConnectTimeout", "ReadTimeout", "RemoteProtocolError",
    "TimeoutException", "ConnectionError", "Timeout",
}


def status_of(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    resp = getattr(exc, "response", None)
    try:
        return int(getattr(resp, "status_code", None))
    except (TypeError, ValueError):
        return None


def _quota(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in ("insufficient_quota", "credit balance", "insufficient balance",
                                  "exceeded your current quota", "billing", "payment required",
                                  "insufficient credits", "out of credits"))


def is_transient(exc: BaseException) -> bool:
    if _quota(exc):
        return False
    if type(exc).__name__ in _TRANSIENT_NAMES:
        return True
    return status_of(exc) in (408, 409, 425, 429, 500, 502, 503, 504, 529)


def explain(provider: str, exc: BaseException) -> str:
    """One sentence a person can act on, for logs, the app's Test button and the fallback."""
    name = providers.get(provider)["name"]
    code = status_of(exc)
    raw = str(exc).strip().replace("\n", " ")
    if len(raw) > 220:
        raw = raw[:217] + "…"
    if _quota(exc) or code == 402:
        return (f"{name} says the account has no credit. Add money in its billing page "
                f"({providers.get(provider).get('billingUrl') or 'the dashboard'}), then try again.")
    if code == 401 or "invalid api key" in raw.lower() or "invalid x-api-key" in raw.lower() \
            or "incorrect api key" in raw.lower():
        return f"{name} rejected the key. Check it was copied whole, or make a new one."
    if code == 403:
        return f"{name} refused: this key isn't allowed to use that model ({raw})."
    if code == 404 or "model_not_found" in raw.lower() or "does not exist" in raw.lower():
        return f"{name} doesn't know that model name. Press Refresh to see the ones your key can use."
    if code == 429:
        return f"{name} is rate-limiting this key. Wait a minute, or raise the account's tier."
    if code and code >= 500:
        return f"{name} is having trouble right now ({code}). Try again shortly."
    return f"{name}: {type(exc).__name__}: {raw}"


def split_system(messages: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, str]]]:
    system_parts, turns = [], []
    for m in messages or []:
        if (m.get("role") or "").lower() == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        else:
            turns.append(m)
    if not turns:
        turns = [{"role": "user", "content": ""}]
    return "\n\n".join(system_parts), turns


def role(turn: Dict[str, str]) -> str:
    r = (turn.get("role") or "user").lower()
    return "assistant" if r in ("assistant", "model") else "user"


def alternate(turns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """user/assistant strictly alternating, starting with user, no empty content.
    core.py sometimes sends two user turns in a row (a tool result, then the question)."""
    out: List[Dict[str, str]] = []
    for t in turns:
        r, c = role(t), (t.get("content") or "").strip()
        if out and out[-1]["role"] == r:
            out[-1]["content"] = (out[-1]["content"] + "\n\n" + c).strip()
        else:
            out.append({"role": r, "content": c})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(conversation continues)"})
    for t in out:
        if not t["content"]:
            t["content"] = "…"
    return out


def estimate_tokens(system: str, turns: Iterable[Dict[str, str]]) -> int:
    n = len(system or "")
    for t in turns:
        n += len(t.get("content") or "")
    return max(1, n // 4)


def cancelled(cancel) -> bool:
    return cancel is not None and bool(getattr(cancel, "is_set", lambda: False)())


class CloudBackend:
    """Backend protocol for core.Reedus. Subclasses set PROVIDER and implement _once()."""

    PROVIDER = ""
    CLOUD = True
    PROFILE: List[Dict] = []

    def __init__(self, model: str, provider_id: Optional[str] = None):
        self.provider = provider_id or self.PROVIDER
        self.model = model
        self._retries = int(os.environ.get("REEDUS_LLM_RETRIES", "2"))
        self._timeout = float(os.environ.get("REEDUS_LLM_TIMEOUT", "45"))
        # request fields this model/API turned out not to accept, learned from 400s
        self._dropped: set = set()

    # --- Backend protocol ---
    def router_ready(self) -> bool:
        return True

    def close(self) -> None:
        self._client = None

    def warm(self) -> float:
        t0 = time.perf_counter()
        try:
            self._once("", [{"role": "user", "content": "hi"}], 1, is_router=True,
                       on_token=None, cancel=None, temperature=None, use_adapter=False)
        except Exception:                                        # noqa: BLE001
            pass
        return time.perf_counter() - t0

    def chat(self, messages: List[Dict[str, str]], max_tokens: int,
             use_adapter: bool = True, temperature: Optional[float] = None,
             on_token: Optional[Callable[[str], None]] = None,
             tier: Optional[str] = None, name: str = "", cancel=None) -> str:
        if budget.exhausted():
            raise CloudUnavailable(
                f"monthly cloud budget of ${budget.cap():.2f} is spent "
                f"(${budget.spent():.2f} used)", provider="budget", retryable=False)
        system, turns = split_system(messages)
        is_router = tier == "router"
        if use_adapter and not is_router:
            shots = persona_shots.block()
            if shots:
                system = f"{system}\n\n{shots}" if system else shots
        last: Optional[BaseException] = None
        for attempt in range(self._retries + 1):
            t0 = time.perf_counter()
            try:
                text, n_in, n_out, searches = self._once(
                    system, turns, int(max_tokens), is_router=is_router, on_token=on_token,
                    cancel=cancel, temperature=temperature, use_adapter=use_adapter)
            except CloudUnavailable:
                raise
            except Exception as e:                               # noqa: BLE001
                last = e
                if not is_transient(e) or attempt >= self._retries or cancelled(cancel):
                    break
                time.sleep(0.35 * (2 ** attempt))
                continue
            self._record(n_in or estimate_tokens(system, turns),
                         n_out or estimate_tokens("", [{"content": text}]), searches)
            if os.environ.get("REEDUS_PROFILE"):
                self.PROFILE.append({"wall_s": time.perf_counter() - t0, "prompt_tokens": n_in,
                                     "completion_tokens": n_out, "tier": "router" if is_router else "main",
                                     "name": name, "system_head": (system or "")[:48],
                                     "searches": searches, "cancelled": cancelled(cancel)})
            return (text or "").strip()
        raise CloudUnavailable(explain(self.provider, last) if last else "no reply",
                               provider=self.provider,
                               retryable=is_transient(last) if last else True)

    def _record(self, n_in: int, n_out: int, searches: int) -> None:
        try:
            budget.record_tokens(self.model, int(n_in), int(n_out))
            if searches:
                budget.record_searches(self.provider, int(searches))
        except Exception:                                        # noqa: BLE001
            pass

    def _once(self, system: str, turns: List[Dict[str, str]], max_tokens: int, *,
              is_router: bool, on_token, cancel, temperature, use_adapter
              ) -> Tuple[str, int, int, int]:
        raise NotImplementedError

    # --- shared by subclasses ---
    def _call(self, fn: Callable[[dict], object], kw: dict, optional: Dict[str, Tuple[str, ...]]):
        """fn(kwargs) with fields the API rejects learned and dropped, once each.
        optional maps a kwarg name to substrings that, in a 400's message, mean 'not this one'."""
        for _ in range(len(optional) + 1):
            clean = {k: v for k, v in kw.items() if k not in self._dropped}
            try:
                return fn(clean)
            except TypeError as e:
                # an older SDK that doesn't know the keyword at all
                bad = re.search(r"unexpected keyword argument '([^']+)'", str(e))
                if bad and bad.group(1) in optional and bad.group(1) not in self._dropped:
                    self._drop(bad.group(1), str(e))
                    continue
                raise
            except Exception as e:                               # noqa: BLE001
                if status_of(e) != 400:
                    raise
                msg = str(e).lower()
                hit = next((k for k, needles in optional.items()
                            if k in clean and any(n in msg for n in needles)), None)
                if not hit:
                    raise
                self._drop(hit, str(e))
        raise CloudUnavailable(f"{self.provider}: too many rejected parameters",
                               provider=self.provider, retryable=False)

    def _drop(self, field: str, why: str) -> None:
        self._dropped.add(field)
        print(f"[reedus] {self.provider}: {self.model} rejects {field!r}, "
              f"leaving it out from now on ({why[:140]})", flush=True)
