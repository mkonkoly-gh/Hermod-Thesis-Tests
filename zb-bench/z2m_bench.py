"""
z2m-layer observer bench. Runs inside the thesis pod. Pure observer:
subscribes to MQTT before the z2m container starts, then the caller
starts the container externally (via host podman). Records:

  - cold start: from --launch-file timestamp until bridge/state=online
  - bridge topic fanout: interval between first and last bridge/* topic
  - permit_join round-trip: publish bridge/request/permit_join, time
    until matching bridge/response/permit_join arrives

Driven by run_z2m_bench.sh.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import paho.mqtt.client as mqtt

SCRIPT_DIR = Path(__file__).resolve().parent


class Collector:
    def __init__(self, host: str, port: int, base_topic: str) -> None:
        self.host = host
        self.port = port
        self.base_topic = base_topic
        self.events: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=f"zbbench-obs-{int(time.time()*1000)}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.state_online_ts: float | None = None
        self.first_topic_ts: float | None = None
        self.last_topic_ts: float | None = None
        self.seen_topics: Dict[str, Dict[str, Any]] = {}
        # Allow a caller to register a "done" Event+topic for one-shot
        # request/response timing without opening a second MQTT client.
        self._watch_topic: str | None = None
        self._watch_event: threading.Event | None = None
        self._watch_payload: str | None = None
        self._watch_ts: float | None = None

    def watch(self, topic: str) -> threading.Event:
        ev = threading.Event()
        self._watch_topic = topic
        self._watch_event = ev
        self._watch_payload = None
        self._watch_ts = None
        return ev

    def watched(self) -> tuple[str | None, float | None]:
        return self._watch_payload, self._watch_ts

    def _on_connect(self, client, _u, _f, _rc):
        # Only listen to bridge/* — the mock-translator and other
        # simulators flood zigbee/<device> topics and swamp the paho
        # dispatcher, making us drop bridge/response messages.
        client.subscribe(f"{self.base_topic}/bridge/#")

    def _on_message(self, _c, _u, msg) -> None:
        ts = time.monotonic()
        retained = getattr(msg, "retain", False)
        with self._lock:
            self.events.append({"topic": msg.topic, "ts": ts,
                                "size": len(msg.payload), "retain": retained})
            if msg.topic not in self.seen_topics:
                self.seen_topics[msg.topic] = {
                    "ts": ts, "size": len(msg.payload), "retain": retained,
                }
                if self.first_topic_ts is None:
                    self.first_topic_ts = ts
                self.last_topic_ts = ts
            # Count the first state=online we see (retained or fresh).
            # The wrapper clears retained topics before starting z2m, so
            # any retained state=online we receive here is from THIS
            # run and is valid; a retained message whose delivery ts is
            # soon after subscribe still marks "z2m has come up".
            if msg.topic == f"{self.base_topic}/bridge/state":
                try:
                    p = json.loads(msg.payload)
                    if p.get("state") == "online" and self.state_online_ts is None:
                        self.state_online_ts = ts
                except Exception:
                    pass
            # One-shot request/response timing hook
            if self._watch_topic and msg.topic == self._watch_topic:
                self._watch_payload = msg.payload.decode("utf-8", "replace")
                self._watch_ts = ts
                print(f"[watch] hit {msg.topic} retain={retained} ts={ts}",
                       flush=True)
                if self._watch_event:
                    self._watch_event.set()

    def start(self) -> None:
        self.client.connect(self.host, self.port, 30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def permit_join_roundtrip(host: str, port: int, base_topic: str,
                          timeout: float = 15.0) -> Dict[str, Any]:
    resp_topic = f"{base_topic}/bridge/response/permit_join"
    seen_resp: Dict[str, Any] = {}
    done = threading.Event()
    events: List[Dict[str, Any]] = []

    def on_msg(_c, _u, msg):
        # Only record events on the bridge subtree we care about — the
        # broker may be busy with other traffic (e.g. Hermod sync).
        if msg.topic.startswith(f"{base_topic}/bridge/"):
            events.append({"t": time.monotonic(), "topic": msg.topic,
                           "size": len(msg.payload),
                           "preview": msg.payload[:120].decode("utf-8", "replace")})
        if msg.topic == resp_topic:
            seen_resp["ts"] = time.monotonic()
            seen_resp["payload"] = msg.payload.decode("utf-8", "replace")
            done.set()

    def on_sub(_c, _u, mid, granted):
        events.append({"t": time.monotonic(), "suback": mid, "granted": list(granted)})

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                          client_id=f"zbbench-req-{int(time.time()*1000)}")
    client.on_message = on_msg
    client.on_subscribe = on_sub
    # Subscribe to everything interesting so we capture the whole conversation
    def _on_connect(c, _u, _f, _rc):
        c.subscribe(f"{base_topic}/bridge/#", 0)
    client.on_connect = _on_connect
    client.connect(host, port, 30)
    client.loop_start()
    # Wait for SUBACK before publishing
    t_sub_start = time.monotonic()
    while time.monotonic() - t_sub_start < 3.0:
        if any("suback" in e for e in events):
            break
        time.sleep(0.05)

    t0 = time.monotonic()
    client.publish(f"{base_topic}/bridge/request/permit_join",
                   json.dumps({"value": True, "time": 5}))
    ok = done.wait(timeout=timeout)
    t1 = seen_resp.get("ts", time.monotonic())
    client.loop_stop()
    client.disconnect()
    return {
        "latency_ms": round((t1 - t0) * 1000, 2) if ok else None,
        "payload": seen_resp.get("payload"),
        "events": events,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-topic", default="zigbee")
    ap.add_argument("--name", default="bench")
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=11883)
    ap.add_argument("--launch-file", required=True,
                    help="File containing the monotonic-equivalent launch timestamp")
    ap.add_argument("--start-timeout", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: runs/z2m-<UTC>.json next to this script")
    args = ap.parse_args()
    if args.out is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        args.out = SCRIPT_DIR / "runs" / f"z2m-{args.name}-{ts}.json"
    args.out.parent.mkdir(parents=True, exist_ok=True)

    launch_epoch = float(Path(args.launch_file).read_text().strip())
    # We can't read the caller's monotonic clock; convert via current offset
    our_mono = time.monotonic()
    our_epoch = time.time()
    launch_mono = our_mono - (our_epoch - launch_epoch)

    col = Collector(args.mqtt_host, args.mqtt_port, args.base_topic)
    col.start()

    deadline = launch_mono + args.start_timeout
    while time.monotonic() < deadline:
        if col.state_online_ts is not None:
            break
        time.sleep(0.2)

    cold_start = None
    if col.state_online_ts is not None:
        cold_start = round(col.state_online_ts - launch_mono, 2)

    # Capture every bridge topic we saw, with whether it was retained
    # (= z2m had already published it before we subscribed) and time
    # relative to the fresh state=online moment.
    post_online_waits: Dict[str, Any] = {}
    if col.state_online_ts is not None:
        time.sleep(3.0)
        for topic, meta in col.seen_topics.items():
            if f"{args.base_topic}/bridge/" in topic:
                post_online_waits[topic] = {
                    "delta_from_state_online_s":
                        round(meta["ts"] - col.state_online_ts, 2),
                    "size": meta["size"],
                    "retained": meta.get("retain", False),
                }

    result = {
        "name": args.name,
        "launch_epoch": launch_epoch,
        "cold_start_s": cold_start,
        "topic_fanout_s":
            round(col.last_topic_ts - col.first_topic_ts, 2)
            if col.first_topic_ts and col.last_topic_ts else None,
        "topics_after_online": post_online_waits,
    }

    if col.state_online_ts is not None:
        # Extra settle time: z2m publishes state=online early, then
        # continues initialising (stack start, channel scan, topic
        # republish). Command round-trip is reliable only after that
        # settles. Measured ~15s in practice (matching manual probe).
        time.sleep(15.0)

        resp_topic = f"{args.base_topic}/bridge/response/permit_join"
        ev = col.watch(resp_topic)

        # Separate publish client to avoid any single-client dispatch
        # contention between receive loop and publish.
        pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                           client_id=f"zbbench-pub-{int(time.time()*1000)}")
        pub.connect(args.mqtt_host, args.mqtt_port, 30)
        pub.loop_start()
        time.sleep(0.3)  # connect ack

        t_pub = time.monotonic()
        mi = pub.publish(
            f"{args.base_topic}/bridge/request/permit_join",
            json.dumps({"value": True, "time": 5}),
            qos=0,
        )
        try:
            mi.wait_for_publish(5.0)
        except Exception:
            pass
        ok = ev.wait(timeout=15.0)
        payload, ts = col.watched()
        pub.loop_stop()
        pub.disconnect()

        result["permit_join_roundtrip"] = {
            "latency_ms": round((ts - t_pub) * 1000, 2) if ok and ts else None,
            "payload": payload,
            "publish_rc": mi.rc,
            "publish_ack": mi.is_published(),
        }
        with col._lock:
            recent = [e for e in col.events if e["ts"] > t_pub - 0.2]
        result["permit_join_events"] = [
            {"topic": e["topic"], "rel_s": round(e["ts"] - t_pub, 3),
             "size": e["size"], "retain": e.get("retain")}
            for e in recent[:40]
        ]

    col.stop()
    args.out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    return 0 if col.state_online_ts else 1


if __name__ == "__main__":
    sys.exit(main())
