"""python -m reedus_relay                     run the relay (Railway runs this)
python -m reedus_relay account "Your name" 10  make an account with $10 in its pool; prints the app token
python -m reedus_relay device <account-id> "Desk Lite"   add a Lite; prints the device token
python -m reedus_relay topup <account-id> 20             add $20 to the pool
python -m reedus_relay accounts                          list accounts and balances

the CLI talks straight to the database (DATABASE_URL), so on Railway run it with
`railway run python -m reedus_relay …` or from the service's shell.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


def _setup() -> None:
    # the brain code lives in the main repo; Docker puts it on PYTHONPATH, a checkout needs this
    try:
        import reedus_companion  # noqa: F401
    except ImportError:
        repo = Path(__file__).resolve().parents[3]
        sys.path.insert(0, str(repo / "reedus_llm"))
    from reedus_relay import config
    config.DATA.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("REEDUS_MODE", "cloud")
    # the relay's keys come from its environment only, never a stray memory/llm.json
    os.environ.setdefault("REEDUS_LLM_FILE", str(config.DATA / "llm.json"))
    os.environ.setdefault("REEDUS_CLOUD_LEDGER", str(config.DATA / "cloud_ledger.json"))
    from reedus_companion import mode
    mode.set_current(mode.CLOUD)
    from reedus_relay import meter, vault
    meter.install()
    vault.load()          # keys saved on the website override Railway variables


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    _setup()
    from reedus_relay import accounts
    if argv and argv[0] == "account":
        out = accounts.create_account(argv[1] if len(argv) > 1 else "Reedus",
                                      float(argv[2]) if len(argv) > 2 else 0.0)
        print(f"account {out['account']['id']}  balance ${out['account']['balance_usd']:.2f}")
        print(f"app token (shown once): {out['token']}")
        return
    if argv and argv[0] == "device":
        out = accounts.create_device(argv[1], argv[2] if len(argv) > 2 else "Reedus Lite")
        print(f"device {out['device']['id']} ({out['device']['name']})")
        print(f"device token (goes in firmware src/secrets.h as LITE_TOKEN, shown once): {out['token']}")
        return
    if argv and argv[0] == "topup":
        print(f"balance ${accounts.top_up(argv[1], float(argv[2])):.2f}")
        return
    if argv and argv[0] == "accounts":
        from reedus_relay.db import db
        for r in db().all("SELECT id, name, balance_usd, spent_usd FROM accounts ORDER BY created"):
            print(f"{r['id']}  {r['name']:20s}  balance ${r['balance_usd']:.2f}  spent ${r['spent_usd']:.2f}")
        return
    if argv:
        print(__doc__)
        return
    asyncio.run(_run())


async def _run() -> None:
    from reedus_relay import config, speech
    from reedus_relay.db import db
    from reedus_relay.server import Hub, serve
    d = db()
    print(f"[relay] database: {'postgres' if d.pg else 'sqlite'}", flush=True)
    print(f"[relay] speech-to-text: {speech.stt_provider()}  end of sentence: {speech.vad_status()}  "
          f"wake: {speech.wake_detector().describe()}",
          flush=True)
    server = await serve(Hub())
    print(f"[relay] listening on {config.HOST}:{config.PORT} (/ website, /lite devices, /app, /health)",
          flush=True)
    asyncio.ensure_future(_reconcile_forever())
    # the voice recognition model, loaded now so the first turn doesn't wait for it
    from reedus_relay import voiceprint
    asyncio.ensure_future(asyncio.to_thread(voiceprint.available))
    await server.serve_forever()


async def _reconcile_forever() -> None:
    """Credit Stripe payments whose payer never came back to the page."""
    from reedus_relay import funding
    while True:
        await asyncio.sleep(30)
        try:
            await asyncio.to_thread(funding.reconcile)
        except Exception as e:                                   # noqa: BLE001
            print(f"[relay] top-up reconcile failed: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
