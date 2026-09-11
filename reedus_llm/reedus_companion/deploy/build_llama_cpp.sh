#!/usr/bin/env bash
set -euo pipefail

NVME="${REEDUS_XEON_ROOT:-/mnt/nvme/reedus}"
SRC="${REEDUS_LLAMA_CPP_SRC:-$NVME/src/llama.cpp}"
BIN="${REEDUS_LLAMA_BIN:-$NVME/bin}"
FORCE="${FORCE:-0}"

mkdir -p "$BIN" "$(dirname "$SRC")"

if [[ -x "$BIN/llama-server" && "$FORCE" != "1" ]]; then
  echo "already have $BIN/llama-server, set FORCE=1 to rebuild"
  "$BIN/llama-server" --version 2>/dev/null || true
  exit 0
fi

if [[ ! -d "$SRC/.git" ]]; then
  echo "== clone ggml-org/llama.cpp → $SRC =="
  git clone --depth 1 https://github.com/ggml-org/llama.cpp.git "$SRC"
else
  echo "== update $SRC =="
  git -C "$SRC" fetch --depth 1 origin master || git -C "$SRC" fetch --depth 1 origin main || true
  git -C "$SRC" pull --ff-only || true
fi

echo "== cmake build (GGML_NATIVE + OpenBLAS) =="
# shellcheck disable=SC1091
if [[ -f /mnt/nvme/reedus/miniconda3/etc/profile.d/conda.sh ]]; then
  source /mnt/nvme/reedus/miniconda3/etc/profile.d/conda.sh
  conda activate reedus-serve 2>/dev/null || true
fi
export OPENBLAS_NUM_THREADS=1
BUILD="$SRC/build"
cmake -S "$SRC" -B "$BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_NATIVE=ON \
  -DGGML_BLAS=ON \
  -DGGML_BLAS_VENDOR=OpenBLAS \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_CURL=ON
cmake --build "$BUILD" -j"$(nproc)" --target llama-server llama-cli

install -m 755 "$BUILD/bin/llama-server" "$BIN/llama-server"
if [[ -x "$BUILD/bin/llama-cli" ]]; then
  install -m 755 "$BUILD/bin/llama-cli" "$BIN/llama-cli"
fi

echo
echo "Installed:"
ls -lh "$BIN/llama-server"
echo
echo "Next:"
echo "  bash $NVME/reedus_llm/reedus_companion/deploy/run_llama_servers.sh"
echo "  REEDUS_BACKEND=server bash $NVME/reedus_llm/reedus_companion/deploy/run_voice.sh --wake"
