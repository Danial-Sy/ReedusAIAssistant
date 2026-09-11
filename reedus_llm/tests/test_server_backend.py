#!/usr/bin/env python3
"""llama-server http backend: routing, streaming, cancel. no live server."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reedus_companion.serve.backend_server import ServerBackend  # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        _fails.append(label)


print("[1] tier pick: persona → main, aux → router")
with mock.patch.object(ServerBackend, "_ping", lambda self, *a, **k: None):
    be = ServerBackend(main_url="http://127.0.0.1:8080",
                       router_url="http://127.0.0.1:8081")

url, model, tier = be._pick(True, None, "persona")
check(tier == "main" and "8080" in url, f"persona on main ({tier} {url})")
url, model, tier = be._pick(False, None, "search-router")
check(tier == "router" and "8081" in url, f"router call on router ({tier} {url})")
url, model, tier = be._pick(False, None, "subjects")
check(tier == "main", f"subjects stay on main via KEEP_ON_MAIN ({tier})")
url, model, tier = be._pick(False, "router", "extract")
check(tier == "router", f"tier=router forces small ({tier})")

print("\n[2] non-stream chat posts JSON and returns content")
fake = {
    "choices": [{"message": {"content": "  hello from server  "}}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
}
with mock.patch.object(be, "_post_json", return_value=fake) as post:
    out = be.chat([{"role": "user", "content": "hi"}], 16, use_adapter=True, name="persona")
check(out == "hello from server", f"stripped content ({out!r})")
check(post.called, "posted once")

print("\n[3] stream + cancel stops early")
events = [
    {"choices": [{"delta": {"content": "Yo"}}]},
    {"choices": [{"delta": {"content": " bro"}}]},
    {"choices": [{"delta": {"content": " more"}}]},
]
cancel = threading.Event()
seen = []


def _fake_stream(url, body, cancel_ev):
    for i, ev in enumerate(events):
        if i == 1:
            cancel_ev.set()
        yield ev
        if cancel_ev.is_set():
            return


with mock.patch.object(be, "_stream", side_effect=_fake_stream):
    out = be.chat([{"role": "user", "content": "x"}], 32, use_adapter=True,
                  name="persona", cancel=cancel, on_token=seen.append)
check(cancel.is_set(), "cancel was set during stream")
check("Yo" in out or out.startswith("Yo"), f"partial text returned ({out!r})")
check("more" not in out, f"stopped before later tokens ({out!r})")

print("\n[4] profile rows when REEDUS_PROFILE=1")
import os
os.environ["REEDUS_PROFILE"] = "1"
ServerBackend.PROFILE.clear()
with mock.patch.object(be, "_post_json", return_value=fake):
    be.chat([{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
            8, use_adapter=True, name="persona")
check(len(ServerBackend.PROFILE) == 1, f"one profile row ({ServerBackend.PROFILE})")
check(ServerBackend.PROFILE[0].get("name") == "persona", "named persona")
check(ServerBackend.PROFILE[0].get("tier") == "main", "tier main")

print()
if _fails:
    print(f"{len(_fails)} FAILED:")
    for f in _fails:
        print(f"   - {f}")
    sys.exit(1)
print("ALL PASS")
