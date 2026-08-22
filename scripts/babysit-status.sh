#!/usr/bin/env bash
# Babysit status — one-screen progress summary suitable for a 20-min
# cron line. Prints to stdout and to logs/cron-checkin.log.
#
# Usage:
#   scripts/babysit-status.sh        # quick summary
#   scripts/babysit-status.sh -v     # also show last 5 completed + currently running
#
# Designed to be safe to call concurrently with the long-running
# orchestrator: only reads state/campaign.json.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="$ROOT/state/campaign.json"
LOG="$ROOT/logs/cron-checkin.log"
mkdir -p "$ROOT/logs"

[[ -f "$STATE" ]] || { echo "no campaign state at $STATE"; exit 2; }

VERBOSE=0
[[ "${1:-}" == "-v" || "${1:-}" == "--verbose" ]] && VERBOSE=1

ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
{
    echo "=== checkin $ts ==="
    python3 "$ROOT/scripts/orchestrator.py" --status
    if (( VERBOSE )); then
        echo
        echo "--- recent failures / flakies ---"
        python3 - <<PY
import json, pathlib
state = json.loads(pathlib.Path("$STATE").read_text())
bad = [r for r in state["runs"] if r["status"] in ("failed", "flaky")]
if not bad:
    print("(none)")
else:
    for r in bad[-15:]:
        print(f"  {r['id']:50}  {r['status']:6}  attempts={r['attempts']}  "
              f"prev={len(r.get('previous_attempts', []))}  err={(r.get('error') or '')[:60]}")
PY
    fi
    echo
} | tee -a "$LOG"
