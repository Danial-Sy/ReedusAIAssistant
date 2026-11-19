#!/usr/bin/env bash
# librespot on the run box: makes Reedus Lite a Spotify Connect speaker (lite/music.py).
# optional. without it Lite still talks, it just can't play music out of its own speaker.
#   bash deploy/setup_librespot.sh
# needs Spotify Premium (librespot, like every Connect speaker, only works with Premium).
set -euo pipefail

if command -v librespot >/dev/null 2>&1; then
  echo "librespot already installed: $(command -v librespot)"
  librespot --version 2>/dev/null || true
  exit 0
fi

echo "== build deps =="
sudo apt-get update
sudo apt-get install -y build-essential pkg-config libasound2-dev curl

if ! command -v cargo >/dev/null 2>&1; then
  echo "== rust (rustup, per-user, ~/.cargo) =="
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  # shellcheck disable=SC1091
  source "$HOME/.cargo/env"
fi

echo "== librespot (a few minutes) =="
cargo install librespot --locked

echo
echo "Installed: $(command -v librespot || echo "$HOME/.cargo/bin/librespot")"
echo "If run_lite.sh can't find it, add to xeon.env:"
echo "  export REEDUS_LIBRESPOT=$HOME/.cargo/bin/librespot"
echo
echo "First time only: start run_lite.sh, then on your phone (same Wi-Fi as this box) open"
echo "Spotify, tap the devices button and pick \"Reedus Lite\". The login is cached in"
echo "memory/librespot/, and \"play X\" goes to Lite from then on."
