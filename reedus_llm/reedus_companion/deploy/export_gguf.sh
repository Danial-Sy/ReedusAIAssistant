#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

SRC="${SRC:-$ROOT/artifacts/reedus-merged-fp16}"
NAME="${NAME:-reedus}"
GGUF_DIR="$ROOT/artifacts/gguf"
F16="$GGUF_DIR/$NAME-f16.gguf"
QUANT_TYPE="${QUANT_TYPE:-Q4_K_M}"
QUANT="$GGUF_DIR/$NAME-$QUANT_TYPE.gguf"

: "${LLAMA_CPP:?Set LLAMA_CPP=/path/to/llama.cpp}"
mkdir -p "$GGUF_DIR"
[[ -d "$SRC" ]] || { echo "Missing $SRC, for the merged model run: python -m reedus_companion.deploy.merge_lora" >&2; exit 1; }

echo ">> $SRC -> GGUF f16…"
python "$LLAMA_CPP/convert_hf_to_gguf.py" "$SRC" --outfile "$F16" --outtype f16

QBIN="$LLAMA_CPP/build/bin/llama-quantize"; [[ -x "$QBIN" ]] || QBIN="$LLAMA_CPP/llama-quantize"
echo ">> Quantize -> $QUANT_TYPE…"
"$QBIN" "$F16" "$QUANT" "$QUANT_TYPE"
echo ">> Done: $QUANT"; ls -lh "$QUANT"
echo ">> (delete $F16 to save space)"
