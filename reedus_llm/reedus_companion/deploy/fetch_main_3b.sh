#!/usr/bin/env bash
# fetch Qwen2.5-3B-Instruct Q4_K_M (~1.9 GB). qwen3 has no 3B, and this is the closest
# drop-in that still speaks the qwen chat format. run on the xeon so it lands on the nvme.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# nvme layout when present, else local artifacts
if [[ -d /mnt/nvme/reedus/gguf ]]; then
  DEST="${REEDUS_GGUF_DIR:-/mnt/nvme/reedus/gguf}"
elif [[ -f "$HERE/xeon.env" ]]; then
  # shellcheck disable=SC1091
  source "$HERE/xeon.env" 2>/dev/null || true
  DEST="${REEDUS_GGUF_DIR:-${REEDUS_ROOT:-/mnt/nvme/reedus}/gguf}"
else
  DEST="$(cd "$HERE/../artifacts/gguf" && pwd)"
fi
mkdir -p "$DEST"

OUT="$DEST/Qwen2.5-3B-Instruct-Q4_K_M.gguf"
REPO="${REEDUS_3B_REPO:-bartowski/Qwen2.5-3B-Instruct-GGUF}"
FILE="${REEDUS_3B_FILE:-Qwen2.5-3B-Instruct-Q4_K_M.gguf}"

if [[ -f "$OUT" ]] && [[ "${REEDUS_3B_FORCE:-0}" != "1" ]]; then
  ls -lh "$OUT"
  echo "already present, set REEDUS_3B_FORCE=1 to re-download"
  exit 0
fi

echo ">> downloading $REPO / $FILE"
echo "   → $OUT"

if command -v huggingface-cli >/dev/null 2>&1; then
  huggingface-cli download "$REPO" "$FILE" --local-dir "$DEST" --local-dir-use-symlinks False
elif command -v hf >/dev/null 2>&1; then
  hf download "$REPO" "$FILE" --local-dir "$DEST"
else
  # direct url, bartowski layout
  URL="https://huggingface.co/${REPO}/resolve/main/${FILE}"
  TMP="${OUT}.partial"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --progress-bar -o "$TMP" "$URL"
  else
    wget -O "$TMP" "$URL"
  fi
  mv "$TMP" "$OUT"
fi

# huggingface-cli may nest, normalize the name
if [[ ! -f "$OUT" ]]; then
  FOUND="$(find "$DEST" -name "$FILE" -type f | head -1 || true)"
  if [[ -n "$FOUND" && "$FOUND" != "$OUT" ]]; then
    mv "$FOUND" "$OUT"
  fi
fi

ls -lh "$OUT"
echo
echo "Set in xeon.env (or export before run_voice.sh):"
echo "  export REEDUS_MAIN_GGUF=$OUT"
echo "Then: bash .../run_voice.sh --wake"
echo "Expect ~20–35% faster decode vs 4B on CPU, not 2×. Revert:"
echo "  export REEDUS_MAIN_GGUF=/mnt/nvme/reedus/gguf/base-qwen3-4b-Q4_K_M.gguf"
