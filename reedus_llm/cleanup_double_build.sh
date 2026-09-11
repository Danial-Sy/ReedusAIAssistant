#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
K="${REEDUS_KNOWLEDGE_DIR:-reedus_companion/memory/knowledge}"

echo "  knowledge dir: $K"

if pgrep -f "knowledge.build_fandom" >/dev/null 2>&1; then
  echo "  !!! a build is still running. Stop it first:  kill -INT \$(pgrep -f knowledge.build_fandom)" >&2
  exit 1
fi

# re-verify with a checksum taken now, don't trust a comment to delete 800 MB
for pair in "anime_manga.03 anime_manga.04" "games.03 games.04"; do
  set -- $pair
  [ -f "$K/$2.jsonl" ] || { echo "  $2 already gone, skipping"; continue; }
  echo "  verifying $1 == $2 …"
  for ext in jsonl npy; do
    a=$(md5sum "$K/$1.$ext" | cut -d' ' -f1)
    b=$(md5sum "$K/$2.$ext" | cut -d' ' -f1)
    [ "$a" = "$b" ] || { echo "  !!! $1.$ext and $2.$ext DIFFER, not deleting anything" >&2; exit 1; }
  done
  echo "    identical, removing $2"
  rm -f "$K/$2.jsonl" "$K/$2.jsonl.idx" "$K/$2.npy"
done

if [ -f "$K/.fandom_done" ]; then
  sort -u "$K/.fandom_done" -o "$K/.fandom_done"
  echo "  resume log deduped: $(wc -l < "$K/.fandom_done" | tr -d ' ') wikis"
fi

echo
echo "  done. Now rebuild the indexes for the new corpus:"
echo "      python -m reedus_companion.knowledge.reindex --force"
echo
echo "  (--force because the term table must be rebuilt over the fandom vocabulary, see"
echo "   knowledge/reindex.py:_df_is_stale for why a stale one silently breaks anchoring.)"
