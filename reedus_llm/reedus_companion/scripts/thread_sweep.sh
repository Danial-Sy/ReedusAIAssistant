#!/usr/bin/env bash
# find the thread count that maximises prefill throughput. physical cores is right on the
# xeon (14 identical) but wrong on a hybrid i9: llama.cpp waits at a barrier for the slowest
# thread, so 8 threads on P-cores beats 16 spread across P+E.
#   bash reedus_companion/scripts/thread_sweep.sh [4 6 8 10 12 16]
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1

THREADS=("$@")
if [[ ${#THREADS[@]} -eq 0 ]]; then
  THREADS=(4 6 8 12 16)
fi

echo "== CPU =="
lscpu | grep -E "Model name|^CPU\(s\)|Thread|Core\(s\)|MHz" || true
echo
if lscpu -e=CORE,MAXMHZ 2>/dev/null | awk 'NR>1{print $2}' | sort -u | wc -l | grep -qv '^1$'; then
  echo "  NOTE: cores report different max clocks -> hybrid CPU (P + E cores)."
  echo "  Expect fewer threads to win. This does NOT apply to the Xeon."
  echo
fi

OUT="$HOME/thread_sweep_$(date +%H%M%S).txt"
echo "results -> $OUT"
echo

for t in "${THREADS[@]}"; do
  echo "=== threads=$t ==="
  REEDUS_THREADS="$t" python -m reedus_companion.scripts.profile_turn --no-cache \
      "hey whats up" "how was your day" 2>&1 \
    | grep -E "^\[reedus\]|^TOTAL|call 2:" \
    | sed "s/^/  /" | tee -a "$OUT"
  echo | tee -a "$OUT"
done

echo
echo "== how to read =="
echo "  Compare the 'call 2' times (the ~1300-token persona prefill)."
echo "  Lowest wins. If 8 beats 16 on this box, pin it:"
echo "      export REEDUS_THREADS=8"
echo "  and add it to your shell profile / the launch command."
echo
echo "  Then re-run the cached profile to see the combined effect:"
echo "      python -m reedus_companion.scripts.profile_turn \\"
echo "          'hey whats up' 'how was your day' 'i had a rough day at work'"
