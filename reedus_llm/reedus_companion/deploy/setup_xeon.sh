#!/usr/bin/env bash
set -euo pipefail

NVME="${REEDUS_XEON_ROOT:-/mnt/nvme/reedus}"
PREFIX="$NVME/miniconda3"
ENV_NAME="reedus-serve"
LLM="$NVME/reedus_llm"
OJ="$NVME/reedus_oj"

if [[ "$(hostname)" == *danials-System* ]]; then
  echo "This script is for the Xeon. You are on the build box." >&2
  exit 1
fi

echo "== system packages =="
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git curl \
  espeak-ng portaudio19-dev ffmpeg \
  libopenblas-dev pkg-config \
  v4l-utils libgl1 libglib2.0-0

if [[ ! -x "$PREFIX/bin/conda" ]]; then
  echo "== miniconda → $PREFIX =="
  curl -fsSL https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -o /tmp/miniconda.sh
  bash /tmp/miniconda.sh -b -p "$PREFIX"
  rm -f /tmp/miniconda.sh
fi

# shellcheck disable=SC1091
source "$PREFIX/etc/profile.d/conda.sh"
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main || true
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r || true

if ! conda env list | grep -q "^${ENV_NAME} "; then
  echo "== conda env $ENV_NAME (python 3.11) =="
  conda create -y -n "$ENV_NAME" python=3.11
fi
conda activate "$ENV_NAME"
conda install -y -c conda-forge openblas

echo "== CPU torch (vision only; not CUDA) =="
pip install --upgrade pip wheel setuptools
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

echo "== llama-cpp-python native Broadwell + OpenBLAS =="
export OPENBLAS_NUM_THREADS=1
export CMAKE_ARGS="-DGGML_NATIVE=ON -DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS"
export FORCE_CMAKE=1
pip install --no-binary llama-cpp-python --force-reinstall llama-cpp-python

echo "== serve + voice =="
pip install -r "$LLM/reedus_companion/requirements/serve-cpu.txt"
pip install -r "$LLM/reedus_companion/requirements/voice.txt"
pip install numpy requests

if [[ -f "$OJ/vision/requirements.txt" ]]; then
  echo "== vision (CPU) =="
  pip install -r "$OJ/vision/requirements.txt"
fi

echo
echo "Done. On this box, from now on:"
echo "  source $PREFIX/etc/profile.d/conda.sh"
echo "  conda activate $ENV_NAME"
echo "  source $LLM/reedus_companion/deploy/xeon.env"
echo "  bash $LLM/reedus_companion/deploy/run_voice.sh"
echo
echo "Sanity:"
echo "  cd $LLM && python3 -m reedus_companion.scripts.setup_check"
