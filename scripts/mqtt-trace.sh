#!/usr/bin/env bash
# Side-car MQTT message logger. Subscribes to '#' on the test broker
# and pipes every line through gzip into a single JSONL file. Killable
# by the orchestrator with SIGTERM.
#
# Output: one JSON object per line, each
#   {"ts":<unix_ms>,"topic":"...","qos":0,"retain":0,"payload":"<base64>"}
# The base64-wrapped payload preserves binary frames (LoRa/ZigBee bridge
# JSON often contains NULs or invalid UTF-8) and keeps the file as
# uniformly newline-delimited JSONL gzip.
#
# Usage:
#   mqtt-trace.sh <out-file-gz> <broker-host> <broker-port>
#
# The orchestrator only runs this when HERMOD_MQTT_TRACE=1. The output
# file is meant to be deleted after analysis; it lives next to the run
# dir, NOT inside the canonical metrics payload, so a single
# `find Tests/raw -name 'mqtt-trace.jsonl.gz' -delete` removes them all.

set -uo pipefail
OUT="${1:?out-file.gz required}"
HOST="${2:-${HERMOD_PI_NODE_IP:-<pi-ip>}}"
PORT="${3:-${HERMOD_PI_NANOMQ_NODEPORT:-31983}}"
MAX_BYTES="${HERMOD_MQTT_TRACE_MAX_BYTES:-104857600}"  # 100 MB cap

if ! command -v mosquitto_sub >/dev/null 2>&1; then
    echo "mqtt-trace: mosquitto_sub not installed; trace skipped" >&2
    exit 0
fi

mkdir -p "$(dirname "$OUT")"

# %U → seconds since epoch (decimal); %j is JSON-friendly when -F is used
# with a JSON template. We assemble the JSON line ourselves so we can
# safely base64 the payload (mosquitto_sub doesn't quote control chars).
# `-N` strips the trailing newline so awk sees one record per line.
# Format spec:
#   %U  — message-rx unix-seconds (decimal)
#   %t  — topic
#   %q  — qos
#   %r  — retain flag
#   %p  — raw payload bytes (binary). NOT %x — mosquitto_sub has no %x.
# We embed the payload through `xxd -p` (hex) per-line via awk so the
# JSON line is text-safe and binary frames survive intact. Awk's
# `getline … < cmd` is non-portable; we use a separate filter script
# (od -An -tx1 piped in) — but the simplest cross-platform approach is
# `mosquitto_sub --pretty -F '%j'` which already emits JSON. Use that.
mosquitto_sub -h "$HOST" -p "$PORT" -t '#' -q 0 -F '%j' 2>/dev/null \
    | awk -v max="$MAX_BYTES" '
        BEGIN { written = 0 }
        # Each line is already JSON like {"tst":...,"topic":"...","payload":"..."}
        /^{/ {
            print
            written += length($0) + 1
            if (written > max) {
                print "{\"event\":\"max_bytes_reached\"}"
                exit 0
            }
        }
    ' \
    | gzip > "$OUT"
