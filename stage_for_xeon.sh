#!/usr/bin/env bash
# stage the Xeon bundle on the build box. artifacts/ is ~31 GB, the Xeon needs ~2.4 GB,
# so this collects exactly what moves into one dir for a single rsync.
#   bash stage_for_xeon.sh            build the staging dir
#   bash stage_for_xeon.sh --ship     ...and rsync it to reedus-xeon
# nothing here deletes anything.
set -euo pipefail

ROOT="${REEDUS_ROOT:-$HOME/Reedus}"
STAGE="${REEDUS_STAGE:-$ROOT/_xeon_bundle}"
COMPANION="$ROOT/reedus_llm/reedus_companion"
QUANT="${REEDUS_GGUF_QUANT:-Q4_K_M}"
SHIP=0
[[ "${1:-}" == "--ship" ]] && SHIP=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '   \033[33m! %s\033[0m\n' "$*"; }

[[ -d "$COMPANION" ]] || { echo "Not found: $COMPANION (set REEDUS_ROOT)"; exit 1; }

say "Staging to $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/reedus_llm/reedus_companion/artifacts/gguf" "$STAGE/reedus_oj"

# --- code + config ---
say "1/5  Companion code (excluding artifacts/, caches, venvs)"
rsync -a \
  --exclude 'artifacts' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.venv*' --exclude '.pytest_cache' \
  "$COMPANION/" "$STAGE/reedus_llm/reedus_companion/"

# --- the model ---
say "2/5  GGUF ($QUANT)"
SRC_GGUF="$COMPANION/artifacts/gguf/reedus-$QUANT.gguf"
if [[ -f "$SRC_GGUF" ]]; then
  cp -v "$SRC_GGUF" "$STAGE/reedus_llm/reedus_companion/artifacts/gguf/"
else
  warn "missing $SRC_GGUF, build it with deploy/export_gguf.sh"
fi

# --- his memory ---
say "3/5  Memory (bulletin + episodic DB)"
if [[ -d "$COMPANION/memory" ]]; then
  if command -v sqlite3 >/dev/null && [[ -f "$COMPANION/memory/episodic.sqlite" ]]; then
    sqlite3 "$COMPANION/memory/episodic.sqlite" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
  fi
  rsync -a --exclude '*-journal' --exclude '*-wal' --exclude '*-shm' \
    "$COMPANION/memory/" "$STAGE/reedus_llm/reedus_companion/memory/"
  echo "   memory staged ($(du -sh "$STAGE/reedus_llm/reedus_companion/memory" | cut -f1))"
else
  warn "no memory/ dir, Reedus starts with a blank slate on the Xeon"
fi

# --- vision ---
say "4/5  Vision package"
rsync -a \
  --exclude '__pycache__' --exclude '*.pyc' --exclude 'calib' \
  --exclude '_calib_download' --exclude 'datasets' --exclude 'collected' \
  "$ROOT/reedus_oj/" "$STAGE/reedus_oj/"

if [[ -d "$ROOT/reedus_oj/vision/models" ]]; then
  echo "   compiled models: $(du -sh "$ROOT/reedus_oj/vision/models" | cut -f1)"
else
  warn "vision/models/ does not exist, run build_models.py first, or the Xeon"
  warn "will fall back to base+set_classes (the 6-second cold load)."
fi

# --- manifest ---
say "5/5  Manifest"
{
  echo "Reedus Xeon bundle, staged $(date -Iseconds)"
  echo "quant: $QUANT"
  echo
  echo "sizes:"
  du -sh "$STAGE"/* 2>/dev/null
  echo
  echo "total: $(du -sh "$STAGE" | cut -f1)"
} | tee "$STAGE/MANIFEST.txt"

say "Done, $(du -sh "$STAGE" | cut -f1) staged at $STAGE"

if [[ $SHIP -eq 1 ]]; then
  say "Shipping to reedus-xeon"
  if ! ssh -o BatchMode=yes -o ConnectTimeout=5 reedus-xeon true 2>/dev/null; then
    echo "Cannot reach reedus-xeon. Check ~/.ssh/config (RUNBOOK §1)."
    exit 1
  fi
  rsync -avh --progress "$STAGE/" reedus-xeon:'~/Reedus/'
  say "Shipped."
else
  echo
  echo "When the Xeon is up:  bash stage_for_xeon.sh --ship"
  echo "Or manually:          rsync -avh --progress $STAGE/ reedus-xeon:~/Reedus/"
fi
