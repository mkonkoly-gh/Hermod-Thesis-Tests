#!/usr/bin/env bash
# Per-run wrapper. Spawns the patched run-profile-pi.sh, attaches a
# per-minute sampler side-car, and echoes the result_dir on the last
# stdout line so the orchestrator can pick it up.
#
# Usage:
#   run-one.sh <profile> --tag LABEL --shape ramp|flat
#
# Honors all HERMOD_OVERRIDE_* env vars consumed by run-profile-pi.sh.
# Adds: HERMOD_PI_NAMESPACE (defaults to 'hermod-test'). The patched
# run-profile-pi.sh treats the namespace as a constant — when this var
# is set, we patch the kubectl invocations via a small wrapper.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$TESTS_DIR/.." && pwd)"
HERMOD_REPO="${HERMOD_REPO:-$REPO_ROOT/Hermod}"
RUN_PROFILE_PI="$HERMOD_REPO/scripts/run-profile-pi.sh"
SAMPLER="$SCRIPT_DIR/per-minute-sampler.sh"
TESTS_RAW_DIR="${TESTS_RAW_DIR:-$TESTS_DIR/raw}"

PROFILE="${1:?usage: run-one.sh <profile> [--tag LABEL] [--shape ramp|flat]}"
shift
TAG=""
SHAPE="flat"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)   TAG="$2"; shift 2 ;;
        --shape) SHAPE="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# We force the test runner to write into Tests/raw/ instead of the source
# repo's tests/results/ by setting REPO_ROOT/tests/results to a symlink
# under TESTS_RAW_DIR. The patched run-profile-pi.sh writes to
# $REPO_ROOT/tests/results/<run-id>; we read its last stdout line to
# discover that path, then rsync the dir into TESTS_RAW_DIR and report
# the new location.

# Simpler: capture the run-profile-pi.sh stdout, extract the result dir,
# move the result to Tests/raw/, then echo the new path.

LOG_TMP="$(mktemp)"
SAMPLER_PID=""
TRACE_PID=""
cleanup() {
    [[ -n "$SAMPLER_PID" ]] && kill -TERM "$SAMPLER_PID" 2>/dev/null || true
    [[ -n "$TRACE_PID" ]]   && kill -TERM "$TRACE_PID"   2>/dev/null || true
    rm -f "$LOG_TMP"
}
trap cleanup EXIT INT TERM

# Start the per-minute sampler — writes to Tests/raw/<run-id>/sampler-*.tsv
if [[ -x "$SAMPLER" ]]; then
    "$SAMPLER" "$TESTS_RAW_DIR" "$TAG" &
    SAMPLER_PID=$!
fi

# Optionally start the MQTT message logger. Off by default — turn on
# only for background-characterization profiles (Phase J) where the
# whole point is "what's actually on the wire". Output is gzipped JSONL
# at Tests/raw/<run-id>/mqtt-trace.jsonl.gz; safe to delete after analysis.
TRACER="$(dirname "$0")/mqtt-trace.sh"
if [[ "${HERMOD_MQTT_TRACE:-0}" == "1" && -x "$TRACER" ]]; then
    TRACE_OUT="$TESTS_RAW_DIR/mqtt-trace-${TAG}.jsonl.gz"
    TRACE_HOST="${HERMOD_MQTT_TRACE_HOST:-${HERMOD_PI_NODE_IP:-<pi-ip>}}"
    TRACE_PORT="${HERMOD_MQTT_TRACE_PORT:-${HERMOD_PI_NANOMQ_NODEPORT:-31983}}"
    "$TRACER" "$TRACE_OUT" "$TRACE_HOST" "$TRACE_PORT" &
    TRACE_PID=$!
fi

# Run the patched runner, tee stdout so we can capture the result dir
# without losing live progress.
set +e
"$RUN_PROFILE_PI" "$PROFILE" --tag "$TAG" --shape "$SHAPE" 2>&1 | tee "$LOG_TMP"
RC=${PIPESTATUS[0]}
set -e

[[ -n "$SAMPLER_PID" ]] && kill -TERM "$SAMPLER_PID" 2>/dev/null || true
[[ -n "$TRACE_PID" ]] && kill -TERM "$TRACE_PID" 2>/dev/null || true

# Last line of the runner is the original result dir.
ORIG_DIR=$(tail -1 "$LOG_TMP" | tr -d '[:space:]')
if [[ -z "$ORIG_DIR" || ! -d "$ORIG_DIR" ]]; then
    echo "run-one.sh: could not determine result dir from runner output" >&2
    exit ${RC:-1}
fi

# Move the result dir into Tests/raw/ (atomic — same filesystem). Keep
# the run-id as the directory name so it's still globally unique.
RUN_ID="$(basename "$ORIG_DIR")"
mkdir -p "$TESTS_RAW_DIR"
DEST_DIR="$TESTS_RAW_DIR/$RUN_ID"
mv "$ORIG_DIR" "$DEST_DIR"

# Move sampler output + mqtt trace into the run dir.
shopt -s nullglob
for f in "$TESTS_RAW_DIR/sampler-${TAG}-"* "$TESTS_RAW_DIR/mqtt-trace-${TAG}.jsonl.gz"; do
    mv "$f" "$DEST_DIR/" 2>/dev/null || true
done
shopt -u nullglob

# Drop a small one-line marker on stdout so the orchestrator can grab it.
echo "$DEST_DIR"
exit $RC
