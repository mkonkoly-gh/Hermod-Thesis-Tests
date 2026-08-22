#!/usr/bin/env bash
# Per-minute side-car sampler. Captures node-level + pod-level state
# every 60 s while a run is in progress. Written into a TSV file under
# Tests/raw/sampler-<TAG>-*.tsv; run-one.sh moves it into the run dir
# once the run finishes.
#
# Usage (typically backgrounded by run-one.sh):
#   per-minute-sampler.sh <TESTS_RAW_DIR> <TAG>

set -u
RAW_DIR="${1:?usage: $0 <TESTS_RAW_DIR> <TAG>}"
TAG="${2:?usage: $0 <TESTS_RAW_DIR> <TAG>}"
NS="${HERMOD_PI_NAMESPACE:-hermod-test}"
CTX="${HERMOD_KIND_CTX:-pi5-live}"
NODE_IP="${HERMOD_PI_NODE_IP:-<pi-ip>}"
PI_KEY="${HERMOD_PI_SSH_KEY:-$HOME/.hermod-pi/keys/<pi-host>.key}"
PI_USER="${HERMOD_PI_SSH_USER:-ubuntu}"

mkdir -p "$RAW_DIR"
NODE_TSV="$RAW_DIR/sampler-${TAG}-node-stats.tsv"
DESCRIBE_LOG="$RAW_DIR/sampler-${TAG}-pod-describe.log"
COORD_PROC_TSV="$RAW_DIR/sampler-${TAG}-coord-proc.tsv"

METRICS_DIR="$RAW_DIR/sampler-${TAG}-metrics"
mkdir -p "$METRICS_DIR"
# SYS_LOG (mosquitto $SYS subs) intentionally unused; see comment in
# sample_translator_metrics. File NOT created.
Z2M_LOG="$RAW_DIR/sampler-${TAG}-zigbee2mqtt-info.log"
LORA_LOG="$RAW_DIR/sampler-${TAG}-lora2mqtt-status.log"
# NANOMQ_LOG intentionally unused; see comment in sample_translator_metrics.

echo -e "ts\tload1\tload5\tload15\tmem_used_mib\tmem_avail_mib\tdisk_used_pct\tnet_rx_mb\tnet_tx_mb" > "$NODE_TSV"
echo -e "ts\tpod\tphase\trestarts\tready\treason\tlast_termination" > "${RAW_DIR}/sampler-${TAG}-pod-summary.tsv"
echo -e "ts\tpid\tvm_rss_kb\tvm_size_kb\tthreads\trchar\twchar\tread_bytes\twrite_bytes" > "$COORD_PROC_TSV"

ssh_node() {
    if [[ -f "$PI_KEY" ]]; then
        ssh -i "$PI_KEY" -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=no \
            "${PI_USER}@${NODE_IP}" "$@" 2>/dev/null || true
    fi
}

sample_node() {
    local ts; ts=$(date -u +%s)
    local out; out=$(ssh_node "cat /proc/loadavg; free -m | awk '/^Mem:/{print \$3, \$7}'; df -h / | awk 'NR==2{print \$5}'; cat /proc/net/dev | awk '/eth0|wlan0/{rx+=\$2; tx+=\$10} END{print rx, tx}'")
    if [[ -n "$out" ]]; then
        local loadl; loadl=$(echo "$out" | sed -n 1p)
        local mems;  mems=$(echo "$out"  | sed -n 2p)
        local disk;  disk=$(echo "$out"  | sed -n 3p | tr -d '%')
        local net;   net=$(echo "$out"   | sed -n 4p)
        local l1 l5 l15; read -r l1 l5 l15 _ <<<"$loadl"
        local mu ma; read -r mu ma <<<"$mems"
        local rx tx; read -r rx tx <<<"$net"
        printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
            "$ts" "${l1:-NA}" "${l5:-NA}" "${l15:-NA}" \
            "${mu:-NA}" "${ma:-NA}" "${disk:-NA}" \
            "${rx:-NA}" "${tx:-NA}" >> "$NODE_TSV"
    fi
}

sample_pods() {
    local ts; ts=$(date -u +%s)
    {
        echo "=== ts=${ts} ==="
        kubectl --context "$CTX" describe pods -n "$NS" 2>/dev/null
        echo
    } >> "$DESCRIBE_LOG"
    # one-line summary per pod
    kubectl --context "$CTX" get pods -n "$NS" \
        -o 'custom-columns=NAME:.metadata.name,PHASE:.status.phase,RESTARTS:.status.containerStatuses[0].restartCount,READY:.status.containerStatuses[0].ready,REASON:.status.containerStatuses[0].state.waiting.reason,LASTTERMINATION:.status.containerStatuses[0].lastState.terminated.reason' \
        --no-headers 2>/dev/null \
        | awk -v ts="$ts" '{print ts"\t"$1"\t"$2"\t"$3"\t"$4"\t"$5"\t"$6}' \
        >> "${RAW_DIR}/sampler-${TAG}-pod-summary.tsv"
}

scrape_endpoint() {
    # scrape_endpoint <pod-label> <port> <path> <out-file>
    local label="$1" port="$2" path="$3" out="$4"
    local pod
    pod=$(kubectl --context "$CTX" get pods -n "$NS" -l "app=$label" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    [[ -z "$pod" ]] && return
    # The METRICS_DIR can disappear mid-run if run-one.sh's end-of-run mv
    # races the sampler's loop. Recreate before append so we never log a
    # "No such file or directory" warning.
    mkdir -p "$(dirname "$out")" 2>/dev/null
    local ts; ts=$(date -u +%Y%m%dT%H%M%SZ)
    {
        echo "=== ${label} ts=${ts} ==="
        kubectl --context "$CTX" exec -n "$NS" "$pod" -- \
            sh -c "wget -q -O - http://127.0.0.1:${port}${path} 2>/dev/null \
                  || curl -sf http://127.0.0.1:${port}${path} 2>/dev/null \
                  || true"
        echo
    } >> "$out" 2>/dev/null
}

sample_translator_metrics() {
    # Coordinator + LoRa2MQTT both expose Prometheus /metrics on 42069/8080.
    scrape_endpoint hermod-coordinator 42069 /metrics "$METRICS_DIR/coord.prom"
    scrape_endpoint lora2mqtt           8080 /metrics "$METRICS_DIR/lora2mqtt.prom"
    # ZigBee2MQTT has no Prometheus exposition; sample its native JSON
    # frontend status. Useful diagnostics: bridge state, network map size.
    scrape_endpoint zigbee2mqtt 8080 /api/info "$Z2M_LOG"
    # LoRa2MQTT has the legacy /api/status too — keep capturing it so we
    # can correlate Prometheus counters against the status JSON.
    scrape_endpoint lora2mqtt 8080 /api/status "$LORA_LOG"
}

# nanomq HTTP API + broker $SYS probes intentionally NOT here:
# - emqx/nanomq:0.21-slim has no curl/wget so in-pod scrape is empty
# - host has no mosquitto_sub package and we don't install one
# Coord's /metrics already exposes ingest/drop/queue depth — that is
# the authoritative throughput source.

sample_coord_proc() {
    local ts; ts=$(date -u +%s)
    local pod; pod=$(kubectl --context "$CTX" get pods -n "$NS" \
        -l app=hermod-coordinator -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    [[ -z "$pod" ]] && return
    local stat io
    stat=$(kubectl --context "$CTX" exec -n "$NS" "$pod" -- cat /proc/1/status 2>/dev/null || true)
    io=$(kubectl --context "$CTX" exec -n "$NS" "$pod" -- cat /proc/1/io 2>/dev/null || true)
    [[ -z "$stat$io" ]] && return
    local pid vm_rss vm_size threads rchar wchar read_bytes write_bytes
    pid=$(echo "$stat" | awk '/^Pid:/{print $2}')
    vm_rss=$(echo "$stat" | awk '/^VmRSS:/{print $2}')
    vm_size=$(echo "$stat" | awk '/^VmSize:/{print $2}')
    threads=$(echo "$stat" | awk '/^Threads:/{print $2}')
    rchar=$(echo "$io" | awk '/^rchar:/{print $2}')
    wchar=$(echo "$io" | awk '/^wchar:/{print $2}')
    read_bytes=$(echo "$io" | awk '/^read_bytes:/{print $2}')
    write_bytes=$(echo "$io" | awk '/^write_bytes:/{print $2}')
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "$ts" "${pid:-NA}" "${vm_rss:-NA}" "${vm_size:-NA}" "${threads:-NA}" \
        "${rchar:-NA}" "${wchar:-NA}" "${read_bytes:-NA}" "${write_bytes:-NA}" \
        >> "$COORD_PROC_TSV"
}

# Loop until killed. 10s instead of 60s — most run profiles measure for
# 30 s, so a 60 s interval would yield 0–1 samples per rep. 10 s gives
# ~3 samples per rep without flooding kubectl.
SAMPLE_INTERVAL_SEC="${HERMOD_SAMPLER_INTERVAL_SEC:-10}"
while true; do
    sample_node
    sample_pods
    sample_coord_proc
    sample_translator_metrics
    sample_broker_sys
    sleep "$SAMPLE_INTERVAL_SEC"
done
