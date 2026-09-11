#!/usr/bin/env bash
_here="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
while read -r _v; do unset "$_v"; done < <(env | grep -oE '^REEDUS_[A-Z0-9_]+' )
unset OMP_NUM_THREADS OMP_THREAD_LIMIT
# shellcheck disable=SC1091
source "$_here/xeon.env"
echo "[reenv] threads=$REEDUS_THREADS router=$REEDUS_ROUTER_THREADS tts=$REEDUS_TTS_THREADS" \
     "model=$(basename "$REEDUS_MAIN_GGUF")"
echo "[reenv] mode=${REEDUS_MODE:-ask} knowledge=${REEDUS_KNOWLEDGE:-auto}" \
     "local_search=${REEDUS_LOCAL_SEARCH:-auto} inline_search=${REEDUS_GEMINI_INLINE_SEARCH:-auto}"
unset _here _v
