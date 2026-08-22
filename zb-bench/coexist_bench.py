"""
Two-dongle coexistence observer. Waits for both z2m instances
(zigbee/* and zigbee2/*) to report state=online, then fires
permit_join on each and records round-trip latency. Also records any
unexpected state=offline or disconnect events during the test window.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

import paho.mqtt.client as mqtt


class DualCollector:
    def __init__(self, host: str, port: int) -> None:
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                    client_id=f"coex-obs-{int(time.time()*1000)}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.online_ts: Dict[str, float] = {}
        self.offline_events: Dict[str, list] = {"zigbee": [], "zigbee2": []}
        self.response_events: Dict[str, list] = {"zigbee": [], "zigbee2": []}
        self.host = host
        self.port = port
        self._watch: Dict[str, threading.Event] = {}
        self._watch_ts: Dict[str, float] = {}
        self._watch_payload: Dict[str, str] = {}

    def _on_connect(self, c, _u, _f, _rc):
        c.subscribe([("zigbee/bridge/#", 0), ("zigbee2/bridge/#", 0)])

    def _on_message(self, _c, _u, msg):
        ts = time.monotonic()
        top = msg.topic
        payload = msg.payload
        base = top.split("/")[0]
        if top == f"{base}/bridge/state":
            try:
                p = json.loads(payload)
                if p.get("state") == "online" and base not in self.online_ts:
                    self.online_ts[base] = ts
                if p.get("state") == "offline":
                    self.offline_events[base].append(ts)
            except Exception:
                pass
        if top == f"{base}/bridge/response/permit_join":
            self.response_events[base].append(ts)
            self._watch_ts[base] = ts
            self._watch_payload[base] = payload.decode("utf-8", "replace")
            if base in self._watch:
                self._watch[base].set()

    def start(self):
        self.client.connect(self.host, self.port, 30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def wait_online(self, base: str, deadline: float) -> bool:
        while time.monotonic() < deadline:
            if base in self.online_ts:
                return True
            time.sleep(0.1)
        return False

    def permit_join(self, base: str, timeout: float = 15.0) -> Dict[str, Any]:
        self._watch[base] = threading.Event()
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                           client_id=f"coex-pub-{base}-{int(time.time()*1000)}")
        pub.connect(self.host, self.port, 30)
        pub.loop_start()
        time.sleep(0.3)
        t_pub = time.monotonic()
        pub.publish(f"{base}/bridge/request/permit_join",
                     json.dumps({"value": True, "time": 5}))
        ok = self._watch[base].wait(timeout=timeout)
        pub.loop_stop()
        pub.disconnect()
        if ok and base in self._watch_ts:
            return {
                "latency_ms": round((self._watch_ts[base] - t_pub) * 1000, 2),
                "payload": self._watch_payload[base],
            }
        return {"latency_ms": None, "payload": None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    launch_epoch = float(Path(args.launch).read_text().strip())
    launch_mono = time.monotonic() - (time.time() - launch_epoch)

    col = DualCollector("127.0.0.1", 11883)
    col.start()

    # Wait for both to come online (90s budget shared)
    deadline = launch_mono + 90.0
    on_z = col.wait_online("zigbee", deadline)
    on_z2 = col.wait_online("zigbee2", deadline)

    result: Dict[str, Any] = {
        "launch_epoch": launch_epoch,
        "zigbee": {
            "cold_start_s": round(col.online_ts["zigbee"] - launch_mono, 2)
                if on_z else None,
        },
        "zigbee2": {
            "cold_start_s": round(col.online_ts["zigbee2"] - launch_mono, 2)
                if on_z2 else None,
        },
    }

    if on_z and on_z2:
        # Settle before commands
        time.sleep(15.0)
        # Sequential: zigbee first, then zigbee2 (avoid broker self-contention)
        rt_z = col.permit_join("zigbee")
        time.sleep(1.0)
        rt_z2 = col.permit_join("zigbee2")
        result["zigbee"]["permit_join"] = rt_z
        result["zigbee2"]["permit_join"] = rt_z2

    # Record any offline events during the test (sign of cross-interference)
    result["zigbee"]["offline_during_test"] = len(col.offline_events["zigbee"])
    result["zigbee2"]["offline_during_test"] = len(col.offline_events["zigbee2"])

    col.stop()
    Path(args.out).write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
