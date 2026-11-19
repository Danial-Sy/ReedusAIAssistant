#!/usr/bin/env python3
"""run the relay's admin actions from your laptop (through its /app socket, RELAY_ADMIN_TOKEN).

    python ReedusLiteS3/tools/admin.py <relay> <admin token> account "Danial" 10
    python ReedusLiteS3/tools/admin.py <relay> <admin token> device <account-id> "Desk Lite"
    python ReedusLiteS3/tools/admin.py <relay> <admin token> topup <account-id> 20
    python ReedusLiteS3/tools/admin.py <relay> <admin token> accounts

<relay> is wss://your-relay.up.railway.app (or ws://localhost:8080). tokens printed here are
shown once: the app token goes into ReedOS, the device token into firmware src/secrets.h.
needs: pip install websockets
"""
from __future__ import annotations

import asyncio
import json
import sys


async def call(url: str, token: str, req: dict) -> dict:
    import websockets
    url = url.rstrip("/") + ("" if url.rstrip("/").endswith("/app") else "/app")
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        auth = json.loads(await ws.recv())
        if not auth.get("admin"):
            sys.exit("that isn't the relay's admin token (RELAY_ADMIN_TOKEN)")
        await ws.send(json.dumps({"id": 1, **req}))
        return json.loads(await ws.recv())


def main() -> None:
    a = sys.argv[1:]
    if len(a) < 3:
        print(__doc__)
        sys.exit(1)
    url, token, cmd, rest = a[0], a[1], a[2], a[3:]
    if cmd == "account":
        req = {"action": "account.create", "name": rest[0] if rest else "Reedus",
               "balance": float(rest[1]) if len(rest) > 1 else 0.0}
    elif cmd == "device":
        req = {"action": "device.create", "account": rest[0], "name": rest[1] if len(rest) > 1 else "Reedus Lite"}
    elif cmd == "topup":
        req = {"action": "topup", "account": rest[0], "usd": float(rest[1])}
    elif cmd == "accounts":
        req = {"action": "accounts"}
    else:
        print(__doc__)
        sys.exit(1)
    out = asyncio.run(call(url, token, req))
    if out.get("error"):
        sys.exit(out["error"])
    if cmd == "account":
        print(f"account {out['account']['id']}  balance ${out['account']['balance']:.2f}")
        print(f"app token (for ReedOS, shown once): {out['token']}")
    elif cmd == "device":
        print(f"device {out['device']['id']} ({out['device']['name']})")
        print(f"device token (firmware src/secrets.h LITE_TOKEN, shown once): {out['token']}")
    elif cmd == "topup":
        print(f"balance ${out['balance']:.2f}")
    else:
        for r in out["accounts"]:
            print(f"{r['id']}  {r['name']:20s}  balance ${r['balance_usd']:.2f}  spent ${r['spent_usd']:.2f}")


if __name__ == "__main__":
    main()
