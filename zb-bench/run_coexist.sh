#!/usr/bin/env bash
# Two-dongle coexistence bench: start both z2m instances simultaneously
# on different base_topic/channel pairs, wait for both to go online,
# send permit_join to each, compare vs standalone single-dongle numbers.
#
# Usage: run_coexist.sh [ch0] [ch1]
#   e.g. run_coexist.sh 15 20   (far, non-overlapping)
#        run_coexist.sh 15 16   (adjacent, could interfere)
#        run_coexist.sh 15 15   (same channel, RF collision)

set -euo pipefail

CH0="${1:-15}"
CH1="${2:-20}"
TESTS_DIR="${TESTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="$TESTS_DIR/.zb-venv/bin/python3"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="$TESTS_DIR/zb-bench/runs/$RUN_ID"
OUT="$RUN_DIR/coexist-${CH0}-${CH1}.json"

TTY0="${TTY0:-/dev/ttyUSB0}"
TTY1="${TTY1:-/dev/ttyUSB1}"
[[ -e "$TTY0" && -e "$TTY1" ]] || {
  echo "need two ZBDongle-E dongles at $TTY0 and $TTY1" >&2; exit 1; }
[[ -x "$VENV_PY" ]] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }
nc -z 127.0.0.1 11883 2>/dev/null || {
  echo "MQTT broker not reachable on 127.0.0.1:11883 (start with bench-broker.sh)" >&2; exit 1; }

mkdir -p "$RUN_DIR"

podman ps -a --format '{{.Names}}' | grep -E "^z2m-" | xargs -r podman rm -f 2>/dev/null || true
fuser -k "$TTY0" "$TTY1" 2>&1 || true
sleep 2

for tty in "$TTY0" "$TTY1"; do
  "$VENV_PY" - "$tty" <<'PY'
import sys, serial, time
s = serial.Serial(sys.argv[1], 115200, timeout=0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.dtr=True;  s.rts=True;  time.sleep(0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.close()
PY
done
sleep 1

"$VENV_PY" - <<'PY'
import paho.mqtt.client as mqtt, time
captured = set()
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="coex-clr")
c.on_connect = lambda c,u,f,rc: c.subscribe([("zigbee/#", 0), ("zigbee2/#", 0)])
c.on_message = lambda c,u,m: captured.add(m.topic) if m.retain else None
c.connect("127.0.0.1", 11883, 30)
c.loop_start()
time.sleep(2.0)
for t in captured:
    c.publish(t, b"", retain=True, qos=1).wait_for_publish(1)
c.loop_stop()
c.disconnect()
print(f"cleared {len(captured)} retained")
PY

TTYS=( "$TTY0" "$TTY1" )
for i in 0 1; do
  TTY="${TTYS[$i]}"
  if [ $i -eq 0 ]; then BASE=zigbee; CH=$CH0; else BASE=zigbee2; CH=$CH1; fi
  SD="$RUN_DIR/state-coex-tty$i"
  rm -rf "$SD" && mkdir -p "$SD"
  cat > "$SD/configuration.yaml" <<EOF
mqtt:
  server: mqtt://127.0.0.1:11883
  base_topic: $BASE
serial:
  port: $TTY
  adapter: ember
  baudrate: 115200
advanced:
  log_level: info
  log_output: [console]
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  channel: $CH
frontend: false
permit_join: false
homeassistant: false
EOF
  chcon -R unconfined_u:object_r:container_file_t:s0 "$SD" 2>/dev/null || true
done

LAUNCH_TS=$(date +%s.%N)
echo "$LAUNCH_TS" > $RUN_DIR/launch-coex.ts

for i in 0 1; do
  podman run -d --name "z2m-coex-tty$i" \
    --device "${TTYS[$i]}" --group-add=keep-groups --network host \
    -v "$RUN_DIR/state-coex-tty$i:/app/data:rw,z" \
    docker.io/koenkk/zigbee2mqtt:latest >/dev/null
done

"$VENV_PY" "$TESTS_DIR/zb-bench/coexist_bench.py" \
  --launch $RUN_DIR/launch-coex.ts --out "$OUT"

echo "== z2m-coex-tty0 log tail =="
podman logs --tail 5 z2m-coex-tty0 2>&1 | grep -iE 'error|permit|response' | tail
echo "== z2m-coex-tty1 log tail =="
podman logs --tail 5 z2m-coex-tty1 2>&1 | grep -iE 'error|permit|response' | tail

for i in 0 1; do
  podman stop "z2m-coex-tty$i" >/dev/null 2>&1 || true
  podman rm "z2m-coex-tty$i" >/dev/null 2>&1 || true
done

echo "== coexist ch$CH0/ch$CH1 =="
cat "$OUT"
