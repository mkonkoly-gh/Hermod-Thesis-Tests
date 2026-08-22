#!/usr/bin/env bash
# Start (or stop) a local Mosquitto broker on 127.0.0.1:11883 for the bench scripts.
# Mosquitto is used (not NanoMQ) because NanoMQ binds 8083 by default and
# collides with miniflux on this host.
# Usage: bench-broker.sh [start|stop|status]

set -euo pipefail

ACTION="${1:-start}"
NAME="zb-bench-mqtt"
IMAGE="docker.io/eclipse-mosquitto:2"
CONF_DIR="$HOME/.local/state/zb-bench-mqtt"

case "$ACTION" in
  start)
    if podman ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "$NAME already running"
      exit 0
    fi
    mkdir -p "$CONF_DIR"
    cat > "$CONF_DIR/mosquitto.conf" <<'EOF'
listener 11883 0.0.0.0
allow_anonymous true
persistence false
log_dest stdout
EOF
    podman rm -f "$NAME" >/dev/null 2>&1 || true
    podman run -d --name "$NAME" --network host \
      -v "$CONF_DIR/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro,z" \
      "$IMAGE" >/dev/null
    for _ in $(seq 1 20); do
      nc -z 127.0.0.1 11883 2>/dev/null && { echo "$NAME up on 127.0.0.1:11883"; exit 0; }
      sleep 0.25
    done
    echo "$NAME failed to bind 11883" >&2
    podman logs "$NAME" 2>&1 | tail -20 >&2
    exit 1
    ;;
  stop)
    podman rm -f "$NAME" >/dev/null 2>&1 || true
    echo "$NAME stopped"
    ;;
  status)
    if podman ps --format '{{.Names}}' | grep -qx "$NAME"; then
      echo "running"
    else
      echo "stopped"
      exit 1
    fi
    ;;
  *)
    echo "usage: $0 [start|stop|status]" >&2
    exit 2
    ;;
esac
