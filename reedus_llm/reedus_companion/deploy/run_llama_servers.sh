#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/xeon.env"

ROOT="${REEDUS_ROOT:-/mnt/nvme/reedus}"
BIN="${REEDUS_LLAMA_BIN:-$ROOT/bin}"
SERVER="${BIN}/llama-server"
LOGDIR="${REEDUS_LLAMA_LOGDIR:-$ROOT/logs}"
PIDDIR="${REEDUS_LLAMA_PIDDIR:-$ROOT/run}"
mkdir -p "$LOGDIR" "$PIDDIR"

MAIN_GGUF="${REEDUS_MAIN_GGUF:-$ROOT/gguf/base-qwen3-4b-Q4_K_M.gguf}"
ROUTER_GGUF="${REEDUS_ROUTER_MODEL:-$ROOT/gguf/qwen3-1.7b-Q4_K_M.gguf}"
DRAFT_GGUF="${REEDUS_DRAFT_GGUF:-}"

MAIN_URL="${REEDUS_MAIN_URL:-http://127.0.0.1:8080}"
ROUTER_URL="${REEDUS_ROUTER_URL:-http://127.0.0.1:8081}"
MAIN_PORT="$(echo "$MAIN_URL" | sed -n 's/.*:\([0-9][0-9]*\).*/\1/p')"
ROUTER_PORT="$(echo "$ROUTER_URL" | sed -n 's/.*:\([0-9][0-9]*\).*/\1/p')"
MAIN_PORT="${MAIN_PORT:-8080}"
ROUTER_PORT="${ROUTER_PORT:-8081}"

MAIN_THREADS="${REEDUS_LLAMA_MAIN_THREADS:-${REEDUS_THREADS:-12}}"
ROUTER_THREADS="${REEDUS_LLAMA_ROUTER_THREADS:-${REEDUS_ROUTER_THREADS:-4}}"
MAIN_CTX="${REEDUS_N_CTX:-3072}"
ROUTER_CTX="${REEDUS_ROUTER_N_CTX:-1536}"
SPEC="${REEDUS_LLAMA_SPEC:-ngram-simple}"
DRAFT_N="${REEDUS_LLAMA_DRAFT_N:-4}"

CMD="${1:-start}"

healthy() {
  local url="$1"
  curl -sf --max-time 2 "$url/health" >/dev/null 2>&1 \
    || curl -sf --max-time 2 "$url/v1/models" >/dev/null 2>&1
}

kill_pidfile() {
  local pf="$1"
  if [[ -f "$pf" ]]; then
    local pid
    pid="$(cat "$pf" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      sleep 0.5
      kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$pf"
  fi
}

stop_all() {
  kill_pidfile "$PIDDIR/llama-main.pid"
  kill_pidfile "$PIDDIR/llama-router.pid"
  echo "stopped llama-server processes"
}

status_all() {
  if healthy "$MAIN_URL"; then echo "main   OK  $MAIN_URL"; else echo "main   DOWN $MAIN_URL"; fi
  if [[ -n "$ROUTER_URL" ]]; then
    if healthy "$ROUTER_URL"; then echo "router OK  $ROUTER_URL"; else echo "router DOWN $ROUTER_URL"; fi
  fi
}

start_one() {
  local name="$1" port="$2" model="$3" threads="$4" ctx="$5" log="$6" pf="$7"
  shift 7
  local extra=("$@")

  if [[ ! -f "$model" ]]; then
    echo "ERROR: GGUF missing: $model" >&2
    exit 1
  fi
  if healthy "http://127.0.0.1:$port"; then
    echo "[$name] already healthy on :$port"
    return 0
  fi
  if [[ ! -x "$SERVER" ]]; then
    echo "ERROR: $SERVER not found, run deploy/build_llama_cpp.sh first" >&2
    exit 1
  fi

  echo "[$name] starting :$port  model=$(basename "$model") threads=$threads ctx=$ctx ${extra[*]:-}"
  # --host 127.0.0.1 keeps it local-only
  nohup "$SERVER" \
    --host 127.0.0.1 --port "$port" \
    -m "$model" \
    -c "$ctx" \
    -t "$threads" \
    -tb "$threads" \
    -b 512 -ub 512 \
    --jinja \
    "${extra[@]}" \
    >"$log" 2>&1 &
  echo $! >"$pf"

  # wait until healthy, model load is ~30-60s on this cpu
  local i
  for i in $(seq 1 90); do
    if healthy "http://127.0.0.1:$port"; then
      echo "[$name] ready (${i}s)"
      return 0
    fi
    if ! kill -0 "$(cat "$pf")" 2>/dev/null; then
      echo "[$name] died during boot, tail $log:" >&2
      tail -40 "$log" >&2 || true
      exit 1
    fi
    sleep 1
  done
  echo "[$name] timed out waiting for health, see $log" >&2
  exit 1
}

case "$CMD" in
  stop) stop_all; exit 0 ;;
  status) status_all; exit 0 ;;
  restart) stop_all; sleep 1 ;;
  start) ;;
  *) echo "usage: $0 {start|stop|status|restart}" >&2; exit 2 ;;
esac

draft_n_flag() {
  if [[ -x "$SERVER" ]] && "$SERVER" --help 2>&1 | grep -q -- '--spec-draft-n-max'; then
    echo "--spec-draft-n-max"
  else
    echo "--draft"
  fi
}

MAIN_EXTRA=()
case "$SPEC" in
  draft|md)
    if [[ -n "$DRAFT_GGUF" && -f "$DRAFT_GGUF" ]]; then
      MAIN_EXTRA+=(-md "$DRAFT_GGUF" "$(draft_n_flag)" "$DRAFT_N" --parallel 1)
      echo "[spec] draft model $(basename "$DRAFT_GGUF") draft_n=$DRAFT_N ($(draft_n_flag))"
    else
      echo "[spec] REEDUS_LLAMA_SPEC=draft but REEDUS_DRAFT_GGUF missing, falling back to ngram-simple"
      MAIN_EXTRA+=(--spec-type ngram-simple --parallel 1)
    fi
    ;;
  ngram|ngram-simple)
    MAIN_EXTRA+=(--spec-type ngram-simple --parallel 1)
    echo "[spec] ngram-simple"
    ;;
  off|0|none|"")
    echo "[spec] off"
    ;;
  *)
    echo "[spec] unknown REEDUS_LLAMA_SPEC=$SPEC, using ngram-simple"
    MAIN_EXTRA+=(--spec-type ngram-simple --parallel 1)
    ;;
esac

start_one main "$MAIN_PORT" "$MAIN_GGUF" "$MAIN_THREADS" "$MAIN_CTX" \
  "$LOGDIR/llama-main.log" "$PIDDIR/llama-main.pid" "${MAIN_EXTRA[@]+"${MAIN_EXTRA[@]}"}"

if [[ -f "$ROUTER_GGUF" ]]; then
  start_one router "$ROUTER_PORT" "$ROUTER_GGUF" "$ROUTER_THREADS" "$ROUTER_CTX" \
    "$LOGDIR/llama-router.log" "$PIDDIR/llama-router.pid"
else
  echo "[router] skip, no GGUF at $ROUTER_GGUF"
fi

echo
echo "export REEDUS_BACKEND=server"
echo "export REEDUS_MAIN_URL=$MAIN_URL"
echo "export REEDUS_ROUTER_URL=$ROUTER_URL"
echo "Then: bash $HERE/run_voice.sh --wake"
