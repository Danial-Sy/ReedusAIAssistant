#!/usr/bin/env bash
# the voice loop with Reedus Lite as its mic + speaker (lite/serve.py). same preconditions
# and flags as run_voice.sh; Lite defaults to cloud mode and the wake word.
#   bash deploy/run_lite.sh               # waits for the Lite to connect, then boots the brain
#   bash deploy/run_lite.sh --no-wait     # boot now, the Lite can join later
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export REEDUS_ENTRY=reedus_companion.lite.serve
export REEDUS_MODE="${REEDUS_MODE:-cloud}"
export REEDUS_VISION=0
exec bash "$HERE/run_voice.sh" "$@"
