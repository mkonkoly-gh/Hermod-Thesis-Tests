#!/usr/bin/env bash
# Run z2m-layer observer bench against one dongle.
# Usage: run_z2m_bench.sh [tty] [base_topic] [channel] [name]
#   tty defaults to the first Sonoff ZBDongle-E by-id link.

set -euo pipefail

TTY="${1:-/dev/ttyUSB0}"
[[ -e "$TTY" ]] || { echo "TTY not found: $TTY" >&2; exit 1; }
BASE="${2:-zigbee}"
CHAN="${3:-15}"
NAME="${4:-tty0}"

TESTS_DIR="${TESTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="$TESTS_DIR/.zb-venv/bin/python3"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="$TESTS_DIR/zb-bench/runs/$RUN_ID"
STATE_DIR="$RUN_DIR/state-${NAME}"
OUT="$RUN_DIR/z2m-${NAME}.json"
LAUNCH_FILE="$RUN_DIR/launch-${NAME}.ts"

[[ -x "$VENV_PY" ]] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }
nc -z 127.0.0.1 11883 2>/dev/null || {
  echo "MQTT broker not reachable on 127.0.0.1:11883 (start with bench-broker.sh)" >&2; exit 1; }

mkdir -p "$RUN_DIR"
rm -rf "$STATE_DIR" && mkdir -p "$STATE_DIR"

cat > "$STATE_DIR/configuration.yaml" <<EOF
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
  channel: $CHAN
frontend: false
permit_join: false
homeassistant: false
EOF

# Clear retained zigbee/* topics from any previous bench so the observer
# doesn't see stale state=online immediately on subscribe.
"$VENV_PY" - "$BASE" <<'PY'
import sys, paho.mqtt.client as mqtt, time
base = sys.argv[1]
captured = set()
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="zb-clr")
c.on_connect = lambda c,u,f,rc: c.subscribe(f"{base}/#")
c.on_message = lambda c,u,m: captured.add(m.topic) if m.retain else None
c.connect("127.0.0.1", 11883, 30)
c.loop_start()
time.sleep(1.5)
for t in captured:
    c.publish(t, b"", retain=True, qos=1).wait_for_publish(1)
c.loop_stop()
c.disconnect()
print(f"cleared {len(captured)} retained under {base}/#")
PY

# DTR+RTS toggle: required if a previous bellows/zigpy session left the
# NCP mid-DATA. Without it the ASH RST is ignored and z2m hangs on boot.
"$VENV_PY" - "$TTY" <<'PY'
import sys, serial, time
s = serial.Serial(sys.argv[1], 115200, timeout=0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.dtr=True;  s.rts=True;  time.sleep(0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.close()
PY

podman rm -f "z2m-bench-$NAME" >/dev/null 2>&1 || true

date +%s.%N > "$LAUNCH_FILE"
# No --rm: container persists after stop so logs stay inspectable.
podman run -d --name "z2m-bench-$NAME" \
  --device "$TTY" --group-add=keep-groups --network host \
  -v "$STATE_DIR:/app/data:rw,z" \
  docker.io/koenkk/zigbee2mqtt:latest >/dev/null

"$VENV_PY" "$TESTS_DIR/zb-bench/z2m_bench.py" \
  --base-topic "$BASE" --name "$NAME" \
  --launch-file "$LAUNCH_FILE" --out "$OUT" --start-timeout 90

echo "== z2m container logs =="
podman logs "z2m-bench-$NAME" 2>&1 | grep -iE 'permit|request|response|MQTT publish|error' | tail -20 | sed 's/^/  z2m: /'
podman stop "z2m-bench-$NAME" >/dev/null 2>&1 || true
podman rm "z2m-bench-$NAME" >/dev/null 2>&1 || true

echo "== $NAME =="
cat "$OUT"
