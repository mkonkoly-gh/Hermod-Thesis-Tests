#!/usr/bin/env bash
# Apply a Hermod config + Pi-8GB-sized resource limits, wait for rollout,
# then run the queue stress test and save results under a label.
#
# Usage: run_pi_stress.sh <config.json> <label>

set -euo pipefail

CONFIG="${1:?need config json}"
LABEL="${2:?need label}"
TESTS_DIR="${TESTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="$TESTS_DIR/zb-bench/runs/pi-${LABEL}-${RUN_ID}"
VENV_PY="$TESTS_DIR/.zb-venv/bin/python3"
[[ -x "$VENV_PY" ]] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }
[[ -n "${PG_PASSWORD:-}" ]] || { echo "PG_PASSWORD env required" >&2; exit 1; }
mkdir -p "$OUT_DIR"

kubectl -n hermod create configmap hermod-config \
  --from-file="appsettings.Production.json=$CONFIG" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n hermod patch deployment hermod-coordinator --type=json -p='[
  {"op":"replace","path":"/spec/template/spec/containers/0/resources","value":{
    "limits":{"cpu":"1500m","memory":"1536Mi"},
    "requests":{"cpu":"500m","memory":"512Mi"}
  }}
]'

kubectl -n hermod set env deployment/hermod-coordinator "HERMOD_TESTRUN=$LABEL-$(date +%s)"

echo "waiting for rollout…"
kubectl -n hermod rollout status deployment/hermod-coordinator --timeout=180s

ss -ltn 2>&1 | grep -q ":11883" || kubectl -n hermod port-forward svc/nanomq 11883:1883 >/tmp/nanomq-pf.log 2>&1 &
ss -ltn 2>&1 | grep -q ":15432" || kubectl -n hermod port-forward svc/postgres 15432:5432 >/tmp/pg-pf.log 2>&1 &
sleep 3

"$VENV_PY" "$TESTS_DIR/zb-bench/queue_stress.py" \
  --rules 0,10,100,1000,10000 \
  --rates 500,1000,2500,5000,10000,20000 \
  --phase-s 8 \
  --out-dir "$OUT_DIR" 2>&1 | tee "$OUT_DIR/log.txt"

curl -s http://127.0.0.1:42069/metrics > "$OUT_DIR/final-metrics.txt" || true
kubectl -n hermod top pod -l app=hermod-coordinator --no-headers > "$OUT_DIR/final-top.txt" || true
kubectl -n hermod describe pod -l app=hermod-coordinator > "$OUT_DIR/final-describe.txt" || true

echo "== DONE: $LABEL =="
tail -10 "$OUT_DIR/log.txt"
