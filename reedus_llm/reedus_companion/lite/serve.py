"""run the normal voice loop with Reedus Lite as its ears and mouth.

    python -m reedus_companion.lite.serve [voice_loop args...]

same brain, same wake word, same barge-in, same cloud/local switch. the only difference is
that `sounddevice` is the network shim, so the mic is the XVF3800 and the speaker is the
Lite's. extra flags (everything else goes to voice_loop):
    --lite-port N      WebSocket port (REEDUS_LITE_PORT, default 8767)
    --no-wait          start the brain before a device connects
"""
from __future__ import annotations

import os
import sys

DIM, RESET = "\033[2m", "\033[0m"


def _pop_flag(argv: list, name: str) -> bool:
    if name in argv:
        argv.remove(name)
        return True
    return False


def _pop_value(argv: list, name: str):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            val = argv[i + 1]
            del argv[i:i + 2]
            return val
    return None


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    port = _pop_value(argv, "--lite-port")
    no_wait = _pop_flag(argv, "--no-wait")
    if port:
        os.environ["REEDUS_LITE_PORT"] = port

    # the shim has to own `sounddevice` before anything asks for a device
    from reedus_companion.lite import netaudio, protocol
    from reedus_companion.lite.bridge import FaceBridge
    from reedus_companion.lite.hub import LiteHub

    hub = LiteHub(port=protocol.PORT).start()
    netaudio.install(hub)
    os.environ.pop("REEDUS_MIC_DEVICE", None)
    os.environ.pop("REEDUS_TTS_DEVICE", None)

    from reedus_companion.voice import events
    events.subscribe(FaceBridge(hub))

    # Lite is the cloud-voice body (gemini + inworld); --mode / REEDUS_MODE still win, and a
    # failed cloud preflight still drops to local
    if "--mode" not in argv and not os.environ.get("REEDUS_MODE"):
        argv += ["--mode", "cloud"]
    if "--wake" not in argv:
        argv.append("--wake")
    if "--cam" not in argv and "--no-vision" not in argv:
        argv.append("--no-vision")       # the Lite has no camera

    if not no_wait and not hub.connected:
        print(f"{DIM}  [lite] waiting for Reedus Lite to connect "
              f"(--no-wait to skip)…{RESET}", flush=True)
        try:
            hub.wait_connected()
        except KeyboardInterrupt:
            return

    from reedus_companion.voice import voice_loop
    voice_loop._RUNTIME["lite"] = hub
    sys.argv = ["reedus_companion.voice.voice_loop", *argv]
    try:
        voice_loop.main()
    finally:
        hub.stop()


if __name__ == "__main__":
    main()
