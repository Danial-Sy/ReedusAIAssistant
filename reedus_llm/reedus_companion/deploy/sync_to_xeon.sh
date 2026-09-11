#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
COMP="$ROOT/reedus_companion"
HOST="${REEDUS_XEON:-dain@192.168.1.249}"
NVME_ROOT="${REEDUS_XEON_ROOT:-/mnt/nvme/reedus}"
REMOTE_CODE="${REEDUS_XEON_CODE:-$NVME_ROOT/reedus_llm}"
REMOTE_KNOWLEDGE="${REEDUS_XEON_KNOWLEDGE:-$NVME_ROOT/knowledge}"
REMOTE_GGUF="${REEDUS_XEON_GGUF:-$NVME_ROOT/gguf}"
REMOTE_OJ="${REEDUS_XEON_OJ:-$NVME_ROOT/reedus_oj}"
OJ_LOCAL="$(cd "$ROOT/.." && pwd)/reedus_oj"

MODE="${1:-dry-run}"
GO=0
for a in "${@:2}"; do
  case "$a" in
    --go) GO=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

RSYNC=(rsync -avh --progress --human-readable)
if [[ "$GO" -eq 0 ]]; then
  RSYNC+=(--dry-run)
  echo "[dry-run] pass --go to actually transfer"
fi

remote_mkdir() {
  ssh "$HOST" "mkdir -p $(printf '%q' "$1")"
}

need_ssh() {
  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$HOST" 'echo ok' 2>/dev/null | grep -q ok; then
    echo "Cannot SSH to $HOST (password/key?). Example:" >&2
    echo "  REEDUS_XEON=dain@192.168.1.249 $0 $MODE --go" >&2
    exit 1
  fi
}

prepare_root() {
  echo "=== ensure remote dirs on $HOST ($NVME_ROOT) ==="
  if ! remote_mkdir "$NVME_ROOT"; then
    echo >&2
    echo "Failed to create $NVME_ROOT on $HOST." >&2
    echo "On the Xeon, run once:" >&2
    echo "  sudo mkdir -p $NVME_ROOT && sudo chown -R \$USER:\$USER $NVME_ROOT" >&2
    echo "If NVMe mounts elsewhere, check \`df -h\` and set REEDUS_XEON_ROOT." >&2
    exit 1
  fi
  remote_mkdir "$REMOTE_CODE/reedus_companion/memory"
  remote_mkdir "$REMOTE_CODE/tests"
  remote_mkdir "$REMOTE_GGUF"
  remote_mkdir "$REMOTE_KNOWLEDGE"
  remote_mkdir "$REMOTE_OJ"
}

sync_code() {
  echo "=== code → $HOST:$REMOTE_CODE/reedus_companion ==="
  "${RSYNC[@]}" \
    --exclude '.git/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'artifacts/' \
    --exclude 'memory/knowledge/' \
    --exclude 'memory/episodic.sqlite' \
    --exclude 'eval_runs/' \
    --exclude '.pytest_cache/' \
    "$COMP/" \
    "$HOST:$REMOTE_CODE/reedus_companion/"
  if [[ -d "$ROOT/tests" ]]; then
    "${RSYNC[@]}" \
      --exclude '__pycache__/' \
      "$ROOT/tests/" \
      "$HOST:$REMOTE_CODE/tests/"
  fi
}

sync_models() {
  echo "=== models → $HOST:$REMOTE_GGUF ==="
  "${RSYNC[@]}" \
    "$COMP/artifacts/gguf/base-qwen3-4b-Q4_K_M.gguf" \
    "$COMP/artifacts/gguf/qwen3-1.7b-Q4_K_M.gguf" \
    "$HOST:$REMOTE_GGUF/"
}

sync_knowledge() {
  echo "=== knowledge → $HOST:$REMOTE_KNOWLEDGE ==="
  "${RSYNC[@]}" \
    --exclude '__pycache__/' \
    "$COMP/memory/knowledge/" \
    "$HOST:$REMOTE_KNOWLEDGE/"
}

sync_memory() {
  echo "=== memory → $HOST:$REMOTE_CODE/reedus_companion/memory ==="
  "${RSYNC[@]}" \
    "$COMP/memory/topics.json" \
    "$HOST:$REMOTE_CODE/reedus_companion/memory/"
  if [[ -f "$COMP/memory/episodic.sqlite" ]]; then
    "${RSYNC[@]}" \
      "$COMP/memory/episodic.sqlite" \
      "$HOST:$REMOTE_CODE/reedus_companion/memory/"
  fi
}

sync_vision() {
  echo "=== vision (YOLO) → $HOST:$REMOTE_OJ ==="
  if [[ ! -d "$OJ_LOCAL/vision" ]]; then
    echo "skip: $OJ_LOCAL/vision not found locally" >&2
    return 0
  fi
  "${RSYNC[@]}" \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude 'frame.jpg' \
    "$OJ_LOCAL/" \
    "$HOST:$REMOTE_OJ/"
}

sizes() {
  echo "=== local payload sizes ==="
  du -sh \
    "$COMP/artifacts/gguf/base-qwen3-4b-Q4_K_M.gguf" \
    "$COMP/artifacts/gguf/qwen3-1.7b-Q4_K_M.gguf" \
    "$COMP/memory/knowledge" \
    "$COMP/memory/topics.json" \
    "$OJ_LOCAL" \
    2>/dev/null || true
  echo
  echo "Remote layout (NVMe):"
  echo "  $REMOTE_CODE/reedus_companion/   code"
  echo "  $REMOTE_GGUF/                    models (3.5G)"
  echo "  $REMOTE_KNOWLEDGE/               corpus (37G)"
  echo "  $REMOTE_OJ/                      vision (~360M)"
}

case "$MODE" in
  dry-run|sizes)
    sizes
    echo
    echo "Transfer:"
    echo "  $0 all --go            (host: $HOST)"
    ;;
  code|models|knowledge|memory|vision|all)
    need_ssh
    prepare_root
    sizes
    echo
    case "$MODE" in
      code) sync_code ;;
      models) sync_models ;;
      knowledge) sync_knowledge ;;
      memory) sync_memory ;;
      vision) sync_vision ;;
      all)
        sync_code
        sync_models
        sync_knowledge
        sync_memory
        sync_vision
        ;;
    esac
    ;;
  *)
    echo "usage: $0 {dry-run|sizes|code|models|knowledge|memory|vision|all} [--go]" >&2
    exit 2
    ;;
esac

if [[ "$GO" -eq 1 && "$MODE" != "dry-run" && "$MODE" != "sizes" ]]; then
  echo
  echo "Done. On the Xeon:"
  echo "  export REEDUS_KNOWLEDGE_DIR=$REMOTE_KNOWLEDGE"
  echo "  export LD_LIBRARY_PATH=\"\$CONDA_PREFIX/lib:\$LD_LIBRARY_PATH\""
  echo "  export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=14"
  echo "  cd $REMOTE_CODE"
  echo "  CUDA_VISIBLE_DEVICES=\"\" REEDUS_THREADS=14 REEDUS_ROUTER_THREADS=6 \\"
  echo "  REEDUS_PROMPT_CACHE_MB=1024 REEDUS_ROUTER_PROMPT_CACHE_MB=512 REEDUS_N_CTX=3072 \\"
  echo "  python -m reedus_companion.serve.cli --backend gguf \\"
  echo "    --gguf $REMOTE_GGUF/base-qwen3-4b-Q4_K_M.gguf \\"
  echo "    --router-gguf $REMOTE_GGUF/qwen3-1.7b-Q4_K_M.gguf"
fi
