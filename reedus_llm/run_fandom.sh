#!/usr/bin/env bash
# fandom wiki corpus build, staged so a broken scrape can't eat hours:
#   ./run_fandom.sh status|doctor|smoke|build|all
#   doctor (~30s, no writes) checks a real wiki request returns real prose,
#   smoke builds a tiny corpus into /tmp and reads it back, build does the real thing.
set -uo pipefail

cd "$(dirname "$0")"
STAGE="${1:-all}"
SEEDS="reedus_companion/knowledge/seeds/fandom_targeted.txt"
SCRATCH="/tmp/reedus_fandom_smoke"
LOG="fandom_build_$(date +%Y%m%d_%H%M%S).log"
LOCK=".fandom_build.pid"
KNOW="${REEDUS_KNOWLEDGE_DIR:-reedus_companion/memory/knowledge}"

export REEDUS_FANDOM_WORKERS="${REEDUS_FANDOM_WORKERS:-4}"

hr()   { printf '\n%s\n' "======================================================================"; }
say()  { printf '\n>>> %s\n' "$*"; }
die()  { printf '\n!!! %s\n\n' "$*" >&2; exit 1; }

check_env() {
  python - <<'PY' || die "environment is not ready, see above"
import importlib.util, sys
missing = [m for m in ("numpy",) if importlib.util.find_spec(m) is None]
if missing:
    print(f"  MISSING: {', '.join(missing)}"); sys.exit(1)
try:
    import torch
    if torch.cuda.is_available():
        print(f"  embedder: CUDA via sentence-transformers ({torch.cuda.get_device_name(0)})")
        sys.exit(0)
except Exception:
    pass
print("  embedder: CPU  <-- ~18x slower. This run will take days, not hours.")
print("  Fix:  conda activate reedus-train   (the env with CUDA torch)")
print("  Continue anyway with:  REEDUS_ALLOW_CPU=1 ./run_fandom.sh")
sys.exit(0 if __import__("os").environ.get("REEDUS_ALLOW_CPU") else 1)
PY
}

stage_status() {
  hr; say "STATUS"
  echo "  knowledge dir : $KNOW"
  if [ -f "$KNOW/.fandom_done" ]; then
    echo "  already done  : $(wc -l < "$KNOW/.fandom_done" | tr -d ' ') wiki(s)"
    sed 's/^/                  /' "$KNOW/.fandom_done"
    echo
    echo "  NOTE: a wiki in this list is SKIPPED. If the failed run logged wikis that"
    echo "        ingested nothing, clear them or pass --redo, or they stay empty forever:"
    echo "          mv $KNOW/.fandom_done $KNOW/.fandom_done.bak"
  else
    echo "  already done  : nothing, this is a clean first run"
  fi
  for q in "$KNOW"/_quarantine_*; do
    [ -d "$q" ] && echo "  quarantined   : $q  (from the failed run; safe to delete once this succeeds)"
  done
  echo
  echo "  live shards:"
  ls -la "$KNOW"/*.jsonl 2>/dev/null | awk '{printf "    %-44s %10.1f MB\n", $NF, $5/1048576}' || echo "    (none yet)"
}

stage_doctor() {
  hr; say "STAGE 1/3, DOCTOR (no writes, ~30s)"
  python -m reedus_companion.knowledge.fandom_doctor --file "$SEEDS" \
    || die "doctor FAILED. Do not build. Re-run one wiki verbosely to see the text:
         python -m reedus_companion.knowledge.fandom_doctor naruto -v"
  say "doctor passed"
}

stage_smoke() {
  hr; say "STAGE 2/3, SMOKE TEST (real ingest, scratch dir, ~3min)"
  rm -rf "$SCRATCH"; mkdir -p "$SCRATCH"
  echo "  writing to $SCRATCH, your live corpus is NOT touched"

  python -m reedus_companion.knowledge.build_fandom naruto:anime_manga \
      --full --limit 40 --out "$SCRATCH" 2>&1 | tee "$SCRATCH/smoke.log"

  python - "$SCRATCH" <<'PY' || die "smoke test FAILED, do not start the build"
import json, sys
from pathlib import Path

root = Path(sys.argv[1])
jsonl = sorted(root.glob("*.jsonl"))
if not jsonl:
    print("\n  FAIL: no shard files written at all."); sys.exit(1)

rows, per = [], {}
for p in jsonl:
    n = 0
    for line in p.open():
        rows.append(json.loads(line)); n += 1
    per[p.name] = n

print(f"\n  shards written: {len(jsonl)}")
for name, n in sorted(per.items(), key=lambda kv: -kv[1]):
    print(f"    {name:34}{n:>8,} passages")

if len(rows) < 50:
    print(f"\n  FAIL: only {len(rows)} passages from 40 full articles. Expected 300+.")
    print("        Extraction is lossy even though the doctor passed, check --full chunking.")
    sys.exit(1)

for p in jsonl:
    f32 = p.with_suffix(".f32")
    npy = p.with_suffix(".npy")
    src = f32 if f32.exists() else npy
    if not src.exists():
        print(f"\n  FAIL: {p.name} has no vector file beside it."); sys.exit(1)

words = [len(r["text"].split()) for r in rows]
secs  = sum(1 for r in rows if ", " in r.get("title", ""))
print(f"\n  {len(rows):,} passages | {sum(words)//len(words)} words avg | "
      f"{secs:,} are section-level (this is the depth --full buys you)")

print("\n  --- three real passages, read back off disk ---")
for r in rows[:3]:
    print(f"\n  [{r.get('title','?')}]  {r.get('url','')}")
    print(f"    {r['text'][:260]}…")

junk = [j for j in ("v t e", "[edit", "Add category", "Retrieved from")
        if any(j.lower() in r["text"].lower() for r in rows)]
if junk:
    print(f"\n  FAIL: navbox/layout junk survived into the corpus: {junk}"); sys.exit(1)

print("\n  PASS, text is clean, sectioned, and reads back correctly.")
PY
  say "smoke test passed"
}

claim_lock() {
  if [ -f "$LOCK" ]; then
    local old; old=$(cat "$LOCK" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
      die "a build is ALREADY RUNNING as PID $old.

    Starting a second one gives you every passage twice, that is what happened on the
    run that ended in a FileNotFoundError from finalize().

    Watch the running one:   tail -f $(ls -t fandom_build_*.log 2>/dev/null | head -1)
    Stop it cleanly:         kill -INT $old"
    fi
    echo "  (clearing stale lock from PID $old, no such process)"
    rm -f "$LOCK"
  fi
}

stage_build() {
  claim_lock
  hr; say "STAGE 3/3, FULL BUILD (all 20 wikis, full articles)"
  echo "  seeds   : $SEEDS"
  echo "  mode    : --full  (whole articles chunked by section)"
  echo "  workers : $REEDUS_FANDOM_WORKERS"
  echo "  log     : $LOG"
  echo
  echo "  Order is deliberate: naruto runs FIRST, Fortnite (~35k) and GTA (~23k) LAST."
  echo "  Those two are ~40% of the run, so stopping before them keeps everything else."
  echo
  nohup python -u -m reedus_companion.knowledge.build_fandom \
      --file "$SEEDS" --full > "$LOG" 2>&1 &
  PID=$!
  echo "$PID" > "$LOCK"
  ( while kill -0 "$PID" 2>/dev/null; do sleep 5; done; rm -f "$LOCK" ) >/dev/null 2>&1 &
  cat <<EOF

  started as PID $PID

  STOPPING IT, this matters. Passages are held in scratch files that only become
  queryable when the build finalises, so HOW you stop it decides whether you keep
  the hours already spent:

      kill -INT $PID        <-- stops cleanly, KEEPS everything ingested so far
      kill -9   $PID        <-- loses the current wiki; recover with --repair

  Ctrl-C here only stops the log tail. The build keeps running.

  If it ever dies hard (power cut, OOM), nothing is lost permanently:
      python -m reedus_companion.knowledge.build_fandom --repair

EOF
  sleep 2
  tail -f "$LOG"
}

case "$STAGE" in
  status) stage_status ;;
  doctor) stage_doctor ;;
  smoke)  check_env && stage_smoke ;;
  build)  check_env && stage_build ;;
  all)
    stage_status
    stage_doctor
    check_env || die "environment check failed"
    stage_smoke
    hr
    say "Both gates passed. Stage 3 is the multi-hour one."
    read -r -p "    Start the full build? [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "    stopped. run './run_fandom.sh build' when ready."; exit 0; }
    stage_build ;;
  *) die "unknown stage '$STAGE', use: all | status | doctor | smoke | build" ;;
esac
