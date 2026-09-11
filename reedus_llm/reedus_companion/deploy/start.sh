#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

ROOT_GUESS="${REEDUS_ROOT:-/mnt/nvme/reedus}"
if [[ -z "${CONDA_PREFIX:-}" ]] && [[ -f "$ROOT_GUESS/miniconda3/etc/profile.d/conda.sh" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT_GUESS/miniconda3/etc/profile.d/conda.sh"
  conda activate reedus-serve
fi
# shellcheck disable=SC1091
source "$HERE/xeon.env"

if [[ "${1:-}" == "--status" ]]; then
  echo "=== env ==="
  echo "  conda        ${CONDA_DEFAULT_ENV:-(none)}"
  echo "  backend      ${REEDUS_BACKEND}"
  echo "  main gguf    $(basename "${REEDUS_MAIN_GGUF}")"
  echo "  threads      ${REEDUS_THREADS} (router ${REEDUS_ROUTER_THREADS})"
  echo "=== llama servers ==="
  bash "$HERE/run_llama_servers.sh" status || true
  echo "=== wake word ==="
  python -m reedus_companion.voice.wakeword --check || true
  echo "=== searxng ==="
  bash "$HERE/run_searxng.sh" status 2>/dev/null || echo "  (not installed, search falls back to DDG)"
  exit 0
fi

echo "=== [1/3] wake word ==="
python -m reedus_companion.voice.wakeword --check || true

echo
echo "=== [2/3] llama servers ==="
# no-op when already healthy
bash "$HERE/run_llama_servers.sh" start

echo
echo "=== [3/3] voice ==="
exec bash "$HERE/run_voice.sh" --wake "$@"
