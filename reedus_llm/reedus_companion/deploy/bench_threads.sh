#!/usr/bin/env bash
# which -t is actually fastest for decode on this box. starts llama-server on a spare port at
# each thread count and reads predicted_per_second from its own timings.
#   bash reedus_companion/deploy/bench_threads.sh [8 12 14 16]     (default sweep 6..16)
# decode is memory-bandwidth bound so the curve flattens near 14; if 10 and 14 are within ~5%
# take the lower one, it leaves whisper and the mic thread more slack.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/xeon.env" >/dev/null 2>&1
ROOT="${REEDUS_ROOT:-/mnt/nvme/reedus}"
BIN="${REEDUS_LLAMA_BIN:-$ROOT/llama.cpp/build/bin}"
SERVER="$BIN/llama-server"
MODEL="${REEDUS_MAIN_GGUF:-$ROOT/gguf/Qwen2.5-3B-Instruct-Q4_K_M.gguf}"
PORT=18080
CTX="${REEDUS_N_CTX:-3072}"
PROMPT="Tell me, in about a hundred words, what makes a good conversation."

[ -x "$SERVER" ] || { echo "llama-server not found at $SERVER" >&2
                      echo "set REEDUS_LLAMA_BIN to the dir containing it" >&2; exit 1; }
[ -f "$MODEL" ]  || { echo "model not found: $MODEL" >&2; exit 1; }

THREADS=("$@"); [ ${#THREADS[@]} -eq 0 ] && THREADS=(6 8 10 12 14 16)
echo "model : $(basename "$MODEL")"
echo "cpus  : $(OMP_NUM_THREADS= nproc) logical / $(lscpu | awk -F: '/^Core\(s\) per socket/{gsub(/ /,"",$2);print $2}') physical"
printf '\n%-8s %-12s %-12s %s\n' "threads" "decode tok/s" "prefill t/s" "verdict"

best_t=0; best_v=0
for t in "${THREADS[@]}"; do
  "$SERVER" -m "$MODEL" --port "$PORT" -c "$CTX" -t "$t" \
            --spec-type ngram-simple --parallel 1 >/tmp/bench_llama.log 2>&1 &
  pid=$!
  for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1
  done
  out="$(curl -sf "http://127.0.0.1:$PORT/completion" -H 'Content-Type: application/json' \
        -d "{\"prompt\":\"$PROMPT\",\"n_predict\":128,\"temperature\":0}" 2>/dev/null)"
  dec="$(printf '%s' "$out" | grep -oE '"predicted_per_second":[0-9.]+' | head -1 | cut -d: -f2)"
  pre="$(printf '%s' "$out" | grep -oE '"prompt_per_second":[0-9.]+'    | head -1 | cut -d: -f2)"
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [ -z "${dec:-}" ]; then printf '%-8s %-12s %-12s %s\n' "$t" "-" "-" "failed (see /tmp/bench_llama.log)"; continue; fi
  printf '%-8s %-12.2f %-12.2f\n' "$t" "$dec" "${pre:-0}"
  awk -v a="$dec" -v b="$best_v" 'BEGIN{exit !(a>b)}' && { best_v="$dec"; best_t="$t"; }
done

echo
echo "fastest DECODE: -t $best_t at $best_v tok/s"
echo
echo "READ BOTH COLUMNS -- they scale completely differently on this box:"
echo "  * decode  is MEMORY-BANDWIDTH bound: flat, and creeps DOWN as threads"
echo "    rise. Optimising for decode alone tells you to use 6, which is wrong."
echo "  * prefill is COMPUTE bound: scales nearly linearly to the physical core"
echo "    count, then stops dead at the SMT boundary."
echo
echo "Prefill only bites on a prompt-cache MISS (new session, changed system"
echo "prompt, context eviction) -- but on those turns it IS the turn. Trading a"
echo "few percent of decode for most of your prefill is the right side of it."
echo
echo "So: take the highest thread count BEFORE prefill stops improving --"
echo "normally the physical core count -- not the best decode number."
