#!/usr/bin/env bash
# Backfill per-run plots for any passed run dir that doesn't already have
# them. Idempotent — re-runs only on dirs missing plot-throughput.png.
# Used by the babysit cron + once at end-of-campaign.

set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null && pwd)"
PLOTTER="$ROOT/scripts/plot-run.py"

shopt -s nullglob
for d in "$ROOT"/raw/2026*; do
    [[ -d "$d" ]] || continue
    [[ -f "$d/summary.json" && -f "$d/plot-throughput.png" ]] && continue
    [[ ! -f "$d/loadgen-r1.json" ]] && continue   # incomplete
    python3 "$PLOTTER" "$d" 2>&1 | sed "s|^|  |"
    echo "plotted $(basename "$d")"
done
