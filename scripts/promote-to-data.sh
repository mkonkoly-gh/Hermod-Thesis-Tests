#!/usr/bin/env bash
# Promote curated runs from Tests/raw/ → Data/runs/ once the campaign is
# done and the flake report has been reviewed. Idempotent.
#
# Selection: for each (profile, rate, shape) triple in campaign.json, pick
# the passing run with the median throughput across reps. That single run
# becomes Data/runs/pi5/<canonical-tag>/.
#
# Usage:
#   scripts/promote-to-data.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${DATA_ROOT:-$(cd "$ROOT/.." && pwd)/Data}"
RAW="$ROOT/raw"
PROMOTED="$ROOT/promoted"
LOG="$ROOT/state/promotion-log.md"

[[ -d "$RAW" ]] || { echo "no raw runs at $RAW" >&2; exit 2; }

python3 - "$ROOT/state/campaign.json" "$RAW" "$PROMOTED" "$DATA/runs/pi5" "$LOG" <<'PY'
import json, shutil, statistics, sys
from pathlib import Path
state_path, raw_dir, promoted_dir, data_runs, log_path = map(Path, sys.argv[1:6])
state = json.loads(state_path.read_text())

# Group passed runs by (profile, rate_override, shape)
groups: dict[tuple, list[dict]] = {}
for run in state["runs"]:
    if run["status"] != "passed":
        continue
    if not run["result_dir"]:
        continue
    key = (run["profile"], run.get("rate_override"), run["shape"])
    groups.setdefault(key, []).append(run)

# Pick the median-throughput run from each group
log_lines = ["# Promotion log\n\n"]
log_lines.append(f"Generated from `{state_path}`.\n\n")
log_lines.append("| group | n_passed | promoted run | thru med | dst |\n")
log_lines.append("| --- | --- | --- | --- | --- |\n")

promoted_dir.mkdir(parents=True, exist_ok=True)
data_runs.mkdir(parents=True, exist_ok=True)

for key, runs in sorted(groups.items()):
    profile, rate, shape = key
    # Compute median throughput per run from manifest's final_counters /
    # measure_sec; if unavailable fall back to first run.
    def thru(run: dict) -> float:
        m = Path(run["result_dir"]) / "manifest.json"
        if not m.exists(): return 0.0
        d = json.loads(m.read_text())
        fc = d.get("final_counters") or {}
        ingested = fc.get("hermod_messages_ingested_total", 0)
        ms = d.get("measure_sec", 1) or 1
        reps = d.get("repeats_total") or d.get("repeats") or 1
        return ingested / max(ms * reps, 1)

    values = [(thru(r), r) for r in runs]
    values.sort(key=lambda x: x[0])
    chosen = values[len(values) // 2][1]
    src = Path(chosen["result_dir"])
    short = chosen["tag"]
    dst_promoted = promoted_dir / short
    dst_data = data_runs / short
    if dst_promoted.exists(): shutil.rmtree(dst_promoted)
    if dst_data.exists(): shutil.rmtree(dst_data)
    shutil.copytree(src, dst_promoted)
    shutil.copytree(src, dst_data)
    log_lines.append(f"| {profile} @ {rate} ({shape}) | {len(runs)} | {short} | "
                     f"{thru(chosen):.1f} msg/s | runs/pi5/{short} |\n")

log_path.write_text("".join(log_lines))
print(f"promoted {len(groups)} canonical runs → {data_runs}")
print(f"log: {log_path}")
PY

echo
echo "Now refresh thesis figures with:"
echo "  python3 $DATA/scripts/build.py --no-copy"
