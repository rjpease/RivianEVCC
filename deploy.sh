#!/usr/bin/env bash
#
# Deploy origin/main to the live bridge on this Mac, verify it, and roll back
# automatically if the new version doesn't come up healthy.
#
#   ./deploy.sh            deploy (asks nothing; refuses if any check fails)
#   ./deploy.sh --check    run every pre-flight check, change nothing
#
# Run it from the folder the LaunchAgent runs (the live checkout).
#
# Safety:
#   - Refuses unless the checkout is on main with no local changes to tracked
#     files, and origin/main is a fast-forward of it.
#   - Refuses outside 12:05-23:45. A restart before noon makes the window
#     shifter park the charging window at 18:00 until the noon flip, and a
#     restart near midnight could collide with the midnight flip. Override
#     with DEPLOY_ANY_TIME=1 only if you understand that.
#   - Never calls the Rivian API and never POSTs to the bridge. The only Rivian
#     traffic is the bridge's own startup and first window-shifter tick.
#
# Verification after restart (up to DEPLOY_TIMEOUT seconds, default 150):
#   - the log shows "Starting Rivian EVCC Bridge" and no Traceback,
#   - the window shifter's first tick succeeds ("disable window moved to ..."
#     or "charging active"); a "will retry" is tolerated if a later tick
#     succeeds within the timeout,
#   - GET /health returns 200 with "stale": false.
# If any of that fails, the previous commit is restored and the bridge
# restarted on it. Every run is appended to deploy.log next to bridge.log.

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LABEL="${BRIDGE_LABEL:-com.rivian.evcc.bridge}"
LOG_DIR="${BRIDGE_LOG_DIR:-$HOME/Library/Logs/rivian-evcc-bridge}"
LOG="$LOG_DIR/bridge.log"
DEPLOY_LOG="$LOG_DIR/deploy.log"
TIMEOUT="${DEPLOY_TIMEOUT:-150}"
NOW_HHMM="${DEPLOY_NOW_HHMM:-$(date +%H%M)}"          # overridable for tests
RESTART_CMD="${DEPLOY_RESTART_CMD:-launchctl kickstart -k gui/$(id -u)/$LABEL}"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

say()  { printf '%s\n' "$*"; }
fail() { say "REFUSED: $*"; exit 2; }

record() {
    mkdir -p "$LOG_DIR"
    printf '%s %s %s -> %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$USER" \
        "${PREV:0:7}" "${NEW:0:7}" "$1" >> "$DEPLOY_LOG"
}

cd "$REPO"

# Port: read only the bridge-port key from config.json, nothing else.
PORT="${BRIDGE_PORT:-$(python3 -c '
import json
try:
    print(json.load(open("rivian_bridge/config.json")).get("bridge-port", 7071))
except Exception:
    print(7071)')}"
HEALTH_URL="${BRIDGE_HEALTH_URL:-http://127.0.0.1:$PORT/health}"

# Interpreter the LaunchAgent uses, so the compile check matches production.
PY="${BRIDGE_PYTHON:-$(plutil -extract ProgramArguments.0 raw \
    "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || echo python3)}"

# ---------------------------------------------------------------- pre-flight

[[ "$(git rev-parse --abbrev-ref HEAD)" == "main" ]] || fail "not on main"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] \
    || fail "local changes to tracked files:
$(git status --short --untracked-files=no)"
stray="$(git ls-files --others --exclude-standard -- 'rivian_bridge/*.py')"
[[ -z "$stray" ]] || fail "untracked Python in rivian_bridge/ (would go live): $stray"

if [[ "${DEPLOY_ANY_TIME:-0}" != "1" ]] && (( 10#$NOW_HHMM < 1205 || 10#$NOW_HHMM > 2345 )); then
    fail "outside the 12:05-23:45 deploy window (now ${NOW_HHMM}). See header."
fi

git fetch -q origin main
PREV="$(git rev-parse HEAD)"
NEW="$(git rev-parse origin/main)"
if [[ "$PREV" == "$NEW" ]]; then
    say "Nothing to deploy: already at ${PREV:0:7}."
    exit 0
fi
git merge-base --is-ancestor "$PREV" "$NEW" \
    || fail "origin/main (${NEW:0:7}) is not a fast-forward of ${PREV:0:7}"

say "Deploying ${PREV:0:7} -> ${NEW:0:7}:"
git log --oneline "$PREV..$NEW" | sed 's/^/  /'

if (( CHECK_ONLY )); then
    say "Check only: all pre-flight checks passed. Nothing changed."
    exit 0
fi

# ---------------------------------------------------------------- helpers

restart_and_verify() {
    # $1 = "deploy" or "rollback". Returns 0 if the bridge came up healthy.
    local mode="$1" offset deadline new_lines status body tmp
    offset=$(stat -f %z "$LOG" 2>/dev/null || echo 0)
    eval "$RESTART_CMD"
    deadline=$(( $(date +%s) + TIMEOUT ))
    while (( $(date +%s) < deadline )); do
        sleep 5
        # If the log was rotated and shrank, read it from the start.
        (( $(stat -f %z "$LOG" 2>/dev/null || echo 0) < offset )) && offset=0
        new_lines="$(tail -c +$((offset + 1)) "$LOG" 2>/dev/null || true)"
        if grep -q 'Traceback' <<<"$new_lines"; then
            say "  ✗ Traceback after restart:"
            grep -A8 'Traceback' <<<"$new_lines" | tail -9 | sed 's/^/    /'
            return 1
        fi
        grep -q 'Starting Rivian EVCC Bridge' <<<"$new_lines" || continue
        if [[ "$mode" == "deploy" ]] && \
           ! grep -qE 'Window shifter: (disable window moved to|charging active)' <<<"$new_lines"; then
            continue
        fi
        tmp="$(mktemp)"
        status=$(curl -s -m 10 -o "$tmp" -w '%{http_code}' "$HEALTH_URL" || true)
        body="$(cat "$tmp" 2>/dev/null || true)"; rm -f "$tmp"
        if [[ "$status" == "200" ]] && python3 -c '
import json, sys
sys.exit(0 if json.loads(sys.argv[1]).get("stale") is False else 1)' "$body" 2>/dev/null; then
            grep -E 'Starting Rivian|Window shifter|Applying|unchanged' <<<"$new_lines" \
                | sed 's/^/    /'
            say "  ✓ /health 200, not stale"
            return 0
        fi
    done
    say "  ✗ not healthy within ${TIMEOUT}s"
    grep -E 'Starting Rivian|Window shifter|ERROR|failed' <<<"${new_lines:-}" | tail -8 \
        | sed 's/^/    /'
    return 1
}

# ---------------------------------------------------------------- deploy

git merge -q --ff-only "$NEW"
if ! "$PY" -m py_compile rivian_bridge/*.py; then
    say "✗ compile check failed; restoring ${PREV:0:7} (bridge not restarted)"
    git reset -q --hard "$PREV"
    record "FAILED-compile (not restarted)"
    exit 1
fi

say "Restarting $LABEL on ${NEW:0:7} ..."
if restart_and_verify deploy; then
    record "OK"
    say "Deployed ${NEW:0:7}."
    exit 0
fi

say "Rolling back to ${PREV:0:7} ..."
git reset -q --hard "$PREV"
if restart_and_verify rollback; then
    record "FAILED, rolled back"
    say "Rolled back to ${PREV:0:7}; the bridge is healthy on the old version."
else
    record "FAILED, rollback ALSO unhealthy"
    say "!! Rollback restarted but the bridge is NOT healthy. Check $LOG now."
fi
exit 1
