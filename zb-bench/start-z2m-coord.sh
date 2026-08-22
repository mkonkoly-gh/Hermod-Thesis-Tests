#!/usr/bin/env bash
# Start a long-running z2m coordinator on the given dongle and open
# permit_join via MQTT. Used to pair a router-flashed second dongle.
# Usage: start-z2m-coord.sh [tty] [base_topic] [channel]

set -euo pipefail

TTY="${1:-/dev/ttyUSB0}"
BASE="${2:-zigbee}"
CHAN="${3:-15}"
TESTS_DIR="${TESTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_PY="$TESTS_DIR/.zb-venv/bin/python3"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_DIR="$TESTS_DIR/zb-bench/runs/coord-$RUN_ID"
STATE_DIR="$RUN_DIR/state"
NAME="z2m-coord"

[[ -e "$TTY" ]] || { echo "TTY not found: $TTY" >&2; exit 1; }
[[ -x "$VENV_PY" ]] || { echo "venv python missing: $VENV_PY" >&2; exit 1; }
nc -z 127.0.0.1 11883 2>/dev/null || {
  echo "MQTT broker not reachable on 127.0.0.1:11883 (start with bench-broker.sh)" >&2; exit 1; }

mkdir -p "$STATE_DIR"
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
chcon -R unconfined_u:object_r:container_file_t:s0 "$STATE_DIR" 2>/dev/null || true

"$VENV_PY" - "$BASE" <<'PY'
import sys, paho.mqtt.client as mqtt, time
base = sys.argv[1]
captured = set()
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="coord-clr")
c.on_connect = lambda c,u,f,rc: c.subscribe(f"{base}/#")
c.on_message = lambda c,u,m: captured.add(m.topic) if m.retain else None
c.connect("127.0.0.1", 11883, 30)
c.loop_start(); time.sleep(1.5)
for t in captured:
    c.publish(t, b"", retain=True, qos=1).wait_for_publish(1)
c.loop_stop(); c.disconnect()
print(f"cleared {len(captured)} retained")
PY

"$VENV_PY" - "$TTY" <<'PY'
import sys, serial, time
s = serial.Serial(sys.argv[1], 115200, timeout=0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.dtr=True;  s.rts=True;  time.sleep(0.1)
s.dtr=False; s.rts=False; time.sleep(0.1)
s.close()
PY

podman rm -f "$NAME" >/dev/null 2>&1 || true
podman run -d --name "$NAME" \
  --device "$TTY" --group-add=keep-groups --network host \
  -v "$STATE_DIR:/app/data:rw,z" \
  docker.io/koenkk/zigbee2mqtt:latest >/dev/null

echo "$NAME starting on $TTY (channel $CHAN, base $BASE)"
echo "RUN_DIR=$RUN_DIR"
echo "logs: podman logs -f $NAME"
echo "stop: podman rm -f $NAME"

# Wait for state=online before opening permit_join
"$VENV_PY" - "$BASE" <<'PY'
import sys, paho.mqtt.client as mqtt, time, json
base = sys.argv[1]
seen_online = False
def on_msg(c, u, m):
    global seen_online
    if m.topic == f"{base}/bridge/state":
        try:
            if json.loads(m.payload).get("state") == "online":
                seen_online = True
        except Exception:
            pass
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="coord-wait")
c.on_connect = lambda c,u,f,rc: c.subscribe(f"{base}/bridge/state")
c.on_message = on_msg
c.connect("127.0.0.1", 11883, 30); c.loop_start()
deadline = time.monotonic() + 60
while time.monotonic() < deadline and not seen_online:
    time.sleep(0.5)
c.loop_stop(); c.disconnect()
print("online" if seen_online else "TIMEOUT waiting for online", flush=True)
sys.exit(0 if seen_online else 2)
PY

# Settle then open permit_join for 254 s
sleep 8
echo "opening permit_join (254s)…"
"$VENV_PY" - "$BASE" <<'PY'
import sys, paho.mqtt.client as mqtt, time, json
base = sys.argv[1]
c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id="coord-pj")
c.connect("127.0.0.1", 11883, 30); c.loop_start()
time.sleep(0.3)
mi = c.publish(f"{base}/bridge/request/permit_join",
                json.dumps({"value": True, "time": 254}), qos=0)
mi.wait_for_publish(5.0)
time.sleep(1.0)
c.loop_stop(); c.disconnect()
print("permit_join request sent", flush=True)
PY

echo "READY: coord up, permit_join open. Plug in / DTR-pulse the router and watch:"
echo "  podman logs -f $NAME | grep -iE 'interview|joined|paired|router'"
