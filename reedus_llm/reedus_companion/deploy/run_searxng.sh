#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/xeon.env"

ROOT="${REEDUS_ROOT:-/mnt/nvme/reedus}"
CONF_DIR="${REEDUS_SEARXNG_CONF:-$ROOT/searxng}"
TEMPLATE="$HERE/searxng/settings.yml"
NAME="${REEDUS_SEARXNG_NAME:-reedus-searxng}"
IMAGE="${REEDUS_SEARXNG_IMAGE:-docker.io/searxng/searxng:latest}"
URL="${REEDUS_SEARXNG_URL:-http://127.0.0.1:8888}"
PORT="$(echo "$URL" | sed -n 's/.*:\([0-9][0-9]*\).*/\1/p')"
PORT="${PORT:-8888}"

CMD="${1:-start}"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  RT=docker
elif command -v podman >/dev/null 2>&1; then
  RT=podman
elif command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker is installed but the daemon isn't reachable." >&2
  echo "  sudo systemctl enable --now docker" >&2
  echo "  sudo usermod -aG docker \$USER   # then log out and back in" >&2
  exit 1
else
  cat >&2 <<MSG
ERROR: no container runtime on this box (checked docker and podman).

  Install podman, rootless, no daemon, nothing resident when the container is stopped:

      sudo apt update && sudo apt install -y podman
      bash $0 start
      bash $0 boot      # optional: survive a reboot

  (Docker works too: curl -fsSL https://get.docker.com | sudo sh, then
   sudo usermod -aG docker \$USER and log out/in. That costs an always-on daemon
   for one container, which is why podman is the suggestion on a 16 GB box.)

Nothing here is required. Reedus probes this port, finds nothing, and uses DDG.
MSG
  exit 1
fi

running() { "$RT" ps --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"; }
exists()  { "$RT" ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$NAME"; }
api_up()  { curl -sf --max-time 3 "$URL/healthz" >/dev/null 2>&1 \
            || curl -sf --max-time 3 "$URL/" >/dev/null 2>&1; }

json_ok() {
  # the check that matters: html can work while ?format=json 403s (missing search.formats: json)
  curl -sf --max-time 8 "$URL/search?q=test&format=json" 2>/dev/null | head -c 1 | grep -q '{'
}

conf_writable() {
  [[ -w "$CONF_DIR" ]] || return 1
  [[ ! -e "$CONF_DIR/settings.yml" ]] || [[ -w "$CONF_DIR/settings.yml" ]]
}

reclaim_conf() {
  if [[ "$RT" == "podman" ]]; then
    echo "[conf] $CONF_DIR is owned by a container subuid, reclaiming (podman unshare)"
    podman unshare chown -R 0:0 "$CONF_DIR" 2>/dev/null && return 0
    echo "  could not reclaim it. Manual: podman unshare chown -R 0:0 $CONF_DIR" >&2
    return 1
  fi
  echo "[conf] $CONF_DIR is not writable. Manual: sudo chown -R $USER: $CONF_DIR" >&2
  return 1
}

install_conf() {
  local force="${1:-0}"
  mkdir -p "$CONF_DIR" 2>/dev/null || true
  if ! conf_writable; then
    reclaim_conf || exit 1
  fi
  if [[ -f "$CONF_DIR/settings.yml" && "$force" != "1" ]] \
     && ! grep -q '__SECRET_KEY__' "$CONF_DIR/settings.yml"; then
    echo "[conf] keeping existing $CONF_DIR/settings.yml"
    echo "       to apply an updated template:  bash $0 reload"
    return 0
  fi
  if [[ ! -f "$TEMPLATE" ]]; then
    echo "ERROR: template missing: $TEMPLATE (did you sync code?)" >&2
    exit 1
  fi
  local key=""
  if [[ -f "$CONF_DIR/settings.yml" ]]; then
    key="$(sed -n 's/.*secret_key:[[:space:]]*"\([^"]*\)".*/\1/p' \
           "$CONF_DIR/settings.yml" | head -1)"
  fi
  if [[ -z "$key" || "$key" == "__SECRET_KEY__" ]]; then
    key="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "[conf] generated a new secret_key"
  else
    echo "[conf] kept the existing secret_key"
  fi
  sed "s|__SECRET_KEY__|$key|" "$TEMPLATE" > "$CONF_DIR/settings.yml"
  chmod 755 "$CONF_DIR"
  chmod 644 "$CONF_DIR/settings.yml"
  echo "[conf] wrote $CONF_DIR/settings.yml"
}

enable_boot() {
  if [[ "$RT" == "docker" ]]; then
    echo "[boot] docker: --restart unless-stopped already covers this."
    echo "       Confirm the daemon is enabled:  sudo systemctl is-enabled docker"
    return 0
  fi
  echo "[boot] enabling lingering so rootless podman survives logout/reboot"
  loginctl enable-linger "$USER" || {
    echo "  needs: sudo loginctl enable-linger $USER" >&2; return 1; }
  systemctl --user enable --now podman-restart.service || {
    echo "  podman-restart.service unavailable, this podman may be too old." >&2
    echo "  Fallback: systemctl --user enable --now podman.socket, or start it from cron @reboot" >&2
    return 1; }
  echo "[boot] done, $NAME will come back after a reboot"
}

start_it() {
  install_conf
  if running; then
    echo "[searxng] already running"
  else
    if exists; then
      echo "[searxng] starting existing container"
      "$RT" start "$NAME" >/dev/null
    else
      echo "[searxng] creating container from $IMAGE"
      "$RT" run -d --name "$NAME" \
        --restart unless-stopped \
        -p "127.0.0.1:$PORT:8080" \
        -v "$CONF_DIR:/etc/searxng:rw" \
        -e "SEARXNG_BASE_URL=$URL/" \
        "$IMAGE" >/dev/null
    fi
  fi

  # first boot needs ~15s to generate state, curling early gets connection reset
  echo -n "[searxng] waiting for the JSON API"
  local i
  for i in $(seq 1 60); do
    if json_ok; then
      echo ", ready (${i}s)"
      test_it || true
      return 0
    fi
    echo -n "."
    sleep 1
  done
  echo
  if api_up; then
    echo "ERROR: HTML is up but ?format=json is not answering." >&2
    echo "  That is almost always a missing 'search: formats: [html, json]' block." >&2
    echo "  Check $CONF_DIR/settings.yml, then: bash $0 restart" >&2
  else
    echo "ERROR: SearXNG did not come up. Logs:" >&2
    "$RT" logs --tail 40 "$NAME" >&2 || true
  fi
  exit 1
}

list_engines() {
  local tmpc
  tmpc="$(mktemp)"
  curl -sf --max-time 8 "$URL/config" > "$tmpc" 2>/dev/null || true
  python3 - "$tmpc" "$CONF_DIR/settings.yml" <<'PY'
import json, re, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print("  could not read /config, is the container up?"); raise SystemExit(1)
eng = d.get("engines") or []
gen = [e for e in eng if "general" in (e.get("categories") or [])]
on  = sorted(e["name"] for e in gen if e.get("enabled"))
off = sorted(e["name"] for e in gen if not e.get("enabled"))
print("[searxng] engines in the 'general' category (the ones a web lookup uses):")
print(f"  ENABLED  ({len(on)}): " + (", ".join(on) or "(none)"))
print(f"  disabled ({len(off)}): " + (", ".join(off) or "(none)"))

known = {e.get("name") for e in eng}
try:
    want = re.findall(r"^\s*-\s*name:\s*[\"']?([^\"'\n#]+?)[\"']?\s*$",
                      open(sys.argv[2]).read(), re.M)
except OSError:
    want = []
ghosts = [w for w in want if w not in known]
if ghosts:
    print()
    print("  !! settings.yml names this instance does NOT have, silently ignored:")
    for g in ghosts:
        print(f"       {g}")
    print("     Remove them, or use an exact name from the lists above.")
elif want:
    print(f"\n  all {len(want)} names in settings.yml exist in this image")
print()
print("  Enable one by adding to the engines: block in settings.yml,")
print("      - name: <exact name from the disabled list>")
print("        disabled: false")
print("  then: bash run_searxng.sh restart")
PY
  rm -f "$tmpc"
}

test_it() {
  local q="${1:-who won the 2026 world cup final}"
  echo "[searxng] live query ${q@Q}, which engines actually answered:"
  local tmpc tmpb
  tmpc="$(mktemp)"; tmpb="$(mktemp)"
  curl -sf --max-time 8 "$URL/config" > "$tmpc" 2>/dev/null || true
  curl -sfG --max-time 25 --data-urlencode "q=$q" --data-urlencode "format=json" \
       "$URL/search" > "$tmpb" 2>/dev/null || true
  python3 - "$tmpc" "$tmpb" <<'PY'
import json, sys
from collections import Counter
try:
    body = json.load(open(sys.argv[2]))
except Exception as e:
    print(f"  no usable response ({e})"); raise SystemExit(1)
res = body.get("results") or []
answered = Counter()
for r in res:
    for e in (r.get("engines") or [r.get("engine")]):
        if e:
            answered[e] += 1
print(f"  {len(res)} results from {len(answered)} engines")
for e, n in answered.most_common():
    print(f"    {e:<22} {n}")

bad = {}
for item in (body.get("unresponsive_engines") or []):
    if isinstance(item, (list, tuple)) and item:
        bad[item[0]] = item[1] if len(item) > 1 else "?"
    else:
        bad[str(item)] = "?"
if bad:
    print("  errored / refused:")
    for e, why in bad.items():
        print(f"    {e:<22} {why}")

try:
    cfg = json.load(open(sys.argv[1]))
    gen = [e["name"] for e in (cfg.get("engines") or [])
           if "general" in (e.get("categories") or []) and e.get("enabled")]
except Exception:
    gen = []
silent = sorted(set(gen) - set(answered) - set(bad))
if silent:
    print("  enabled but returned nothing for this query:")
    print("    " + ", ".join(silent))

if len(answered) < 2:
    print("  WARNING: fewer than 2 engines answered. The multi-source merge is the whole")
    print("           reason to run this, one engine is DDG with extra steps.")
    print("           Next:  bash run_searxng.sh engines     # names, and typo check")
else:
    print(f"  OK: {len(answered)} independent snippet sources.")
PY
  rm -f "$tmpc" "$tmpb"
}

case "$CMD" in
  start)   start_it ;;
  stop)    running && "$RT" stop "$NAME" >/dev/null && echo "[searxng] stopped" || echo "[searxng] not running" ;;
  restart) running && "$RT" stop "$NAME" >/dev/null || true; "$RT" rm -f "$NAME" >/dev/null 2>&1 || true; start_it ;;
  reload)  install_conf 1; running && "$RT" stop "$NAME" >/dev/null || true
           "$RT" rm -f "$NAME" >/dev/null 2>&1 || true; start_it ;;
  status)
    if running; then echo "container  OK   $NAME ($RT)"; else echo "container  DOWN $NAME ($RT)"; fi
    if api_up;  then echo "http       OK   $URL";        else echo "http       DOWN $URL"; fi
    if json_ok; then echo "json api   OK   $URL/search?format=json"
    else echo "json api   DOWN, check 'search: formats: [html, json]' in $CONF_DIR/settings.yml"; fi
    ;;
  test)    shift || true; test_it "$*" ;;
  engines) list_engines ;;
  boot)    enable_boot ;;
  logs)    "$RT" logs --tail 80 -f "$NAME" ;;
  *) echo "usage: $0 {start|stop|restart|reload|status|test [query]|engines|boot|logs}" >&2; exit 2 ;;
esac

if [[ "$CMD" == "start" || "$CMD" == "restart" || "$CMD" == "reload" ]]; then
  echo
  echo "Reedus picks this up on its own (REEDUS_SEARXNG_URL=$URL, re-probed every"
  echo "REEDUS_SEARXNG_RECHECK_S=${REEDUS_SEARXNG_RECHECK_S:-180}s), no restart needed."
  if [[ "$RT" == "podman" ]]; then
    echo
    echo "Rootless podman has no daemon, so --restart unless-stopped alone does NOT survive"
    echo "a reboot. To make it:  bash $0 boot"
  fi
fi
