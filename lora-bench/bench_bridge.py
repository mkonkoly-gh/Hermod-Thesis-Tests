"""
Bridge-aware packet-size sweep: serial TX on one adapter, lora2mqtt
bridges RX on the other adapter and publishes to `lora/#`. We count
both serial round-trip (bench.py already does that standalone) and
MQTT publish rate via a subscriber.

Compares per-size loss at the adapter layer vs loss through lora2mqtt,
so we can see which breaks first.

Usage:
    tests/.venv/bin/python3 bench_bridge.py \
        --tx /dev/ttyACM1 --rx-mqtt-host localhost --rx-mqtt-port 11883 \
        --out-md /tmp/lora-bridge-report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Any

import paho.mqtt.client as mqtt
import serial

MAGIC = b"HMRDBR"


class MqttCollector:
    """Subscribes to lora/# and stores message arrival times keyed by the
    embedded sequence number parsed from the JSON `data` field. lora2mqtt
    publishes {"data":"...", ...} and the bench-sent raw bytes land
    verbatim in `data` (up to newline framing)."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                   client_id=f"lora-bench-{int(time.time())}")
        self.client.on_message = self._on_msg
        self.client.on_connect = self._on_connect
        self.arrivals: Dict[int, List[Dict[str, Any]]] = {}
        self.other_topics: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _on_connect(self, client, _u, _flags, _rc):
        # Subscribe from the on_connect callback so a reconnect resubscribes
        # automatically; subscribing before loop_start silently drops the
        # SUBSCRIBE packet in paho v1 callback API.
        client.subscribe("lora/#")

    def start(self) -> None:
        self.client.connect(self.host, self.port, 30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def reset(self) -> None:
        with self._lock:
            self.arrivals.clear()
            self.other_topics.clear()

    def _on_msg(self, _c, _u, msg) -> None:
        # Parse the `data` field out of JSON. If parse fails, count into
        # other_topics so we know mock-translator noise vs real bench
        # frames.
        try:
            payload = json.loads(msg.payload)
            data = payload.get("data", "")
            if not isinstance(data, str):
                with self._lock:
                    self.other_topics[msg.topic] = self.other_topics.get(msg.topic, 0) + 1
                return
            raw = data.encode("latin-1", errors="replace")
            idx = raw.find(MAGIC)
            if idx < 0:
                with self._lock:
                    self.other_topics[msg.topic] = self.other_topics.get(msg.topic, 0) + 1
                return
            # Wire shape: `HMRDBR-<seq>-<size>-<body>`. Skip the
            # leading `-` right after the magic so split doesn't
            # produce an empty first element.
            tail = raw[idx + len(MAGIC) + 1:]
            try:
                after = tail.split(b"-", 2)
                seq = int(after[0])
                size = int(after[1])
            except (ValueError, IndexError):
                return
            with self._lock:
                self.arrivals.setdefault(seq, []).append({
                    "ts": time.monotonic(),
                    "size": size,
                    "topic": msg.topic,
                })
        except Exception:
            with self._lock:
                self.other_topics[msg.topic] = self.other_topics.get(msg.topic, 0) + 1


def make_frame(seq: int, size: int) -> bytes:
    # Printable ASCII only. lora2mqtt's serial reader splits on
    # newline and turns everything before it into a JSON string, so
    # binary bytes (0x00, 0x0a, 0x0d) inside a frame would fragment
    # the MQTT message or truncate the payload before the magic
    # prefix is visible. `.` is chosen for the filler because it has
    # no special meaning in any framing layer we cross.
    prefix = MAGIC + f"-{seq}-{size}-".encode("ascii")
    pad_len = max(0, size - len(prefix))
    return prefix + (b"." * pad_len) + b"\n"


def sweep(tx_port: str, baud: int, sizes: List[int], count: int,
          spacing_ms: int, collector: MqttCollector) -> List[Dict[str, Any]]:
    # Send every frame first, then give the bridge generous settle time
    # BEFORE partitioning arrivals by size. The bridge has its own
    # read-loop cadence and will trail real-time; trying to measure
    # per-size in-line misses late arrivals.
    collector.reset()
    all_sends: Dict[int, Dict[str, Any]] = {}
    seq = 0
    tx = serial.Serial(tx_port, baud, timeout=0.5, write_timeout=2.0)
    try:
        for size in sizes:
            for _ in range(count):
                seq += 1
                body = make_frame(seq, size)
                all_sends[seq] = {"size": size, "ts": time.monotonic()}
                tx.write(body)
                tx.flush()
                time.sleep(spacing_ms / 1000.0)
        # Settle: wait enough for the largest pending frame to cross
        # the radio + deserialise + publish. 5 s baseline plus a
        # generous per-byte slop.
        settle = 5.0 + (max(sizes) * 0.05)
        time.sleep(settle)
    finally:
        tx.close()

    # Partition arrivals by size (look up each received seq's size in
    # all_sends). Every size-bucket reports its own loss and latency.
    rows: List[Dict[str, Any]] = []
    for size in sizes:
        sent_seqs = [s for s, meta in all_sends.items() if meta["size"] == size]
        got = [s for s in sent_seqs if s in collector.arrivals]
        lost = [s for s in sent_seqs if s not in collector.arrivals]
        lat_ms = [
            (collector.arrivals[s][0]["ts"] - all_sends[s]["ts"]) * 1000.0
            for s in got
        ]
        rows.append({
            "size_bytes": size,
            "sent": len(sent_seqs),
            "received": len(got),
            "lost": len(lost),
            "loss_pct": round(len(lost) / max(len(sent_seqs), 1) * 100, 2),
            "latency_ms_p50": round(statistics.median(lat_ms), 1) if lat_ms else None,
            "latency_ms_max": round(max(lat_ms), 1) if lat_ms else None,
            "other_mqtt_traffic": dict(collector.other_topics),
        })
    return rows


def render(rows: List[Dict[str, Any]], meta: Dict[str, Any]) -> str:
    lines = ["# LoRa bridge sweep — lora2mqtt pipeline", ""]
    for k, v in meta.items():
        lines.append(f"- {k}: `{v}`")
    lines.append("")
    lines.append("| size | sent | recv via MQTT | lost | loss% | p50 ms | max ms | other topics |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|:---|")
    for r in rows:
        other = ", ".join(f"{k}:{v}" for k, v in r["other_mqtt_traffic"].items()) or "—"
        lines.append(
            f"| {r['size_bytes']} | {r['sent']} | {r['received']} | {r['lost']} |"
            f" {r['loss_pct']} | {r['latency_ms_p50'] or '—'} | {r['latency_ms_max'] or '—'} |"
            f" {other} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--tx", default="/dev/ttyACM1")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--mqtt-host", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=11883)
    ap.add_argument("--sizes", default="16,64,128,200,220")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--spacing-ms", type=int, default=1500)
    ap.add_argument("--out-md", type=Path, default=Path("/tmp/lora-bridge-report.md"))
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    collector = MqttCollector(args.mqtt_host, args.mqtt_port)
    collector.start()
    time.sleep(1.5)
    rows = sweep(args.tx, args.baud, sizes, args.count, args.spacing_ms, collector)
    collector.stop()
    args.out_md.write_text(render(rows, {
        "TX serial": args.tx,
        "baud": args.baud,
        "MQTT": f"{args.mqtt_host}:{args.mqtt_port}",
        "count per size": args.count,
        "spacing": f"{args.spacing_ms} ms",
    }))
    print(f"wrote {args.out_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
