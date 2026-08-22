"""
Hermod ingestion latency probe.

Hermod's MessageProcessor records every consumed message into the
`message_history` table with a `received_at` timestamp. This probe
publishes synthetic messages on `zigbee/<id>` topics, then queries
the database to measure publish→Hermod-ingestion latency.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List

import paho.mqtt.client as mqtt
import psycopg2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--spacing-ms", type=int, default=100)
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=11883)
    ap.add_argument("--pg-host", default="127.0.0.1")
    ap.add_argument("--pg-port", type=int, default=15432)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: runs/hermod-latency-<UTC>.json next to this script")
    args = ap.parse_args()
    if args.out is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        args.out = Path(__file__).resolve().parent / "runs" / f"hermod-latency-{ts}.json"
    args.out.parent.mkdir(parents=True, exist_ok=True)

    marker = f"zbbench-{int(time.time()*1000)}"
    topic_base = f"zigbee/bench_{marker}"

    pub_ts: Dict[int, float] = {}
    pub_wall: Dict[int, float] = {}

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                     client_id=f"herlat-{int(time.time()*1000)}")
    c.connect(args.mqtt_host, args.mqtt_port, 30)
    c.loop_start()

    for seq in range(args.count):
        payload = json.dumps({
            "seq": seq,
            "bench": marker,
            "temperature": 22.0 + (seq % 10) * 0.1,
            "battery": 80,
        })
        pub_ts[seq] = time.monotonic()
        pub_wall[seq] = time.time()
        c.publish(f"{topic_base}_{seq:04d}", payload, qos=0)
        time.sleep(args.spacing_ms / 1000.0)

    time.sleep(3.0)  # settle
    c.loop_stop()
    c.disconnect()

    pg_password = os.environ.get("PG_PASSWORD")
    if not pg_password:
        raise SystemExit("missing required env var: PG_PASSWORD")
    conn = psycopg2.connect(
        host=args.pg_host, port=args.pg_port,
        user=os.environ.get("PG_USER", "postgres"),
        password=pg_password,
        dbname=os.environ.get("PG_DB", "hermod"),
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT topic, EXTRACT(EPOCH FROM received_at) "
        "FROM message_history WHERE topic LIKE %s ORDER BY received_at",
        (f"{topic_base}_%",),
    )
    rows = cur.fetchall()
    conn.close()

    matched: List[float] = []
    for topic, epoch in rows:
        try:
            seq = int(topic.rsplit("_", 1)[-1])
        except ValueError:
            continue
        if seq in pub_wall:
            latency_ms = (float(epoch) - pub_wall[seq]) * 1000
            if latency_ms >= 0:
                matched.append(latency_ms)

    stats = {
        "count_sent": args.count,
        "count_matched": len(matched),
        "loss_pct": round((args.count - len(matched)) / args.count * 100, 2),
        "spacing_ms": args.spacing_ms,
    }
    if matched:
        matched.sort()
        stats["latency_ms_p50"] = round(statistics.median(matched), 2)
        stats["latency_ms_p95"] = round(
            statistics.quantiles(matched, n=20)[18]
            if len(matched) >= 20 else max(matched), 2)
        stats["latency_ms_max"] = round(max(matched), 2)
        stats["latency_ms_min"] = round(min(matched), 2)

    args.out.write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
