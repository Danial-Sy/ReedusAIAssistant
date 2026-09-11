#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/xeon.env"
ROOT="${REEDUS_ROOT:-/mnt/nvme/reedus}"
cd "$ROOT/reedus_llm"

if [[ -n "${REEDUS_PYTHON:-}" ]]; then
  PY="$REEDUS_PYTHON"
elif [[ -n "${REEDUS_CONDA_ENV:-}" ]]; then
  for base in "${REEDUS_CONDA_BASE:-}" "$ROOT/miniconda3" "$ROOT/anaconda3" \
              "$ROOT/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" \
              "$HOME/miniforge3" /opt/conda; do
    [[ -n "$base" && -f "$base/etc/profile.d/conda.sh" ]] || continue
    # shellcheck disable=SC1091
    source "$base/etc/profile.d/conda.sh"
    if conda activate "$REEDUS_CONDA_ENV" 2>/dev/null; then
      CONDA_ROOT_USED="$base"
      PY=python
      break
    fi
  done
  if [[ -z "${PY:-}" ]]; then
    echo "ERROR: could not activate conda env '$REEDUS_CONDA_ENV'." >&2
    echo "  Looked for etc/profile.d/conda.sh under:" >&2
    echo "    \$REEDUS_CONDA_BASE, $ROOT/{miniconda3,anaconda3,miniforge3}," >&2
    echo "    \$HOME/{miniconda3,anaconda3,miniforge3}, /opt/conda" >&2
    echo "  Set REEDUS_CONDA_BASE in xeon.env, or bypass conda with REEDUS_PYTHON." >&2
    exit 1
  fi
  echo "[reedus] conda: $CONDA_ROOT_USED  env=$REEDUS_CONDA_ENV"
fi
: "${PY:=python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "ERROR: interpreter not found: $PY" >&2; exit 1; }

"$PY" - <<'PYCHECK' || exit 1
import importlib.util as u, os, sys
need = ["numpy", "sounddevice", "faster_whisper"]
if os.environ.get("REEDUS_MODE") == "cloud":
    need += ["google.genai", "requests"]
missing = [m for m in need if u.find_spec(m) is None]
print(f"[reedus] python: {sys.executable}")
if missing:
    print(f"ERROR: this interpreter is missing: {', '.join(missing)}\n"
          f"  Install into THIS interpreter:\n"
          f"    {sys.executable} -m pip install -r reedus_companion/requirements/cloud.txt",
          file=sys.stderr)
    sys.exit(1)
PYCHECK

CAM="${REEDUS_CAM:-0}"
MAIN_DEFAULT="$ROOT/gguf/base-qwen3-4b-Q4_K_M.gguf"
MAIN="${REEDUS_MAIN_GGUF:-$MAIN_DEFAULT}"
ROUTER="${REEDUS_ROUTER_MODEL:-$ROOT/gguf/qwen3-1.7b-Q4_K_M.gguf}"
BACKEND="${REEDUS_BACKEND:-gguf}"

case "${REEDUS_MODE:-ask}" in
  cloud) NEED_LOCAL=no ;;
  local) NEED_LOCAL=yes ;;
  *)     NEED_LOCAL=maybe ;;
esac

_local_problem() {   # $1 = message
  if [[ "$NEED_LOCAL" == "yes" ]]; then
    echo "ERROR: $1" >&2
    return 1
  fi
  echo "[reedus] note: $1" >&2
  echo "[reedus] continuing, say \"cloud mode\" at the prompt. " \
       "Local mode will not work until this is fixed." >&2
  return 0
}

if [[ "$NEED_LOCAL" != "no" && "$BACKEND" != "server" && ! -f "$MAIN" ]]; then
  _local_problem "main GGUF missing: $MAIN
  (xeon.env normally pins the 3B; source it first)
  3B: bash $HERE/fetch_main_3b.sh" || exit 1
fi

if [[ "$BACKEND" == "server" ]]; then
  MAIN_URL="${REEDUS_MAIN_URL:-http://127.0.0.1:8080}"
  if [[ "$NEED_LOCAL" != "no" ]] \
     && ! curl -sf --max-time 2 "$MAIN_URL/health" >/dev/null 2>&1 \
     && ! curl -sf --max-time 2 "$MAIN_URL/v1/models" >/dev/null 2>&1; then
    _local_problem "llama-server not reachable at $MAIN_URL
  bash $HERE/build_llama_cpp.sh
  bash $HERE/run_llama_servers.sh" || exit 1
  fi
  echo "[reedus] backend: native llama-server"
  echo "[reedus] main URL: $MAIN_URL"
  echo "[reedus] router URL: ${REEDUS_ROUTER_URL:-(none)}"
  exec "$PY" -m reedus_companion.voice.voice_loop --backend server \
    --cam "$CAM" \
    "$@"
fi

if [[ "$NEED_LOCAL" == "no" ]]; then
  echo "[reedus] cloud mode: skipping local model preconditions"
fi
echo "[reedus] backend: llama-cpp-python (in-process)"
echo "[reedus] main GGUF: $MAIN"
echo "[reedus] router GGUF: $ROUTER"

exec "$PY" -m reedus_companion.voice.voice_loop --backend gguf \
  --gguf "$MAIN" \
  --router-gguf "$ROUTER" \
  --cam "$CAM" \
  "$@"
