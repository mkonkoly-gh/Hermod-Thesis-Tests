"""
End-to-end RF command-behaviour bench for two ZBDongle-E adapters paired via
zigbee2mqtt — one as coordinator (production NCP), one as router
(Itead Z3 Router firmware). Real over-the-air ZCL traffic.

Two sweeps:
  1. RTT/loss vs scheduled rate at a fixed small payload — reads the
     one-byte genBasic.zclVersion attribute. The harness waits for each
     response before submitting the next command, so this is not achieved
     pipelined throughput.
  2. RTT vs payload size at a fixed safe rate — reads multiple genBasic
     attributes per command.

Each MQTT publish is correlated with the next response on the device
topic. The latency we measure is publish_to_broker → response_published
which encompasses: broker → coord-z2m → coord-NCP → ASH → 802.15.4 TX
→ router NCP → router stack → ACK → 802.15.4 TX → coord-NCP → coord-z2m
→ broker → us. The ASH and broker contributions are <1 ms each at
localhost; the rest is the actual radio link.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import paho.mqtt.client as mqtt

SCRIPT_DIR = Path(__file__).resolve().parent


class Probe:
    def __init__(self, host: str, port: int, base: str, device: str) -> None:
        self.base = base
        self.device = device
        self.set_topic = f"{base}/{device}/set"
        self.get_topic = f"{base}/{device}/get"
        self.resp_topic = f"{base}/{device}"
        self._lock = threading.Lock()
        self._waiters: Dict[int, threading.Event] = {}
        self._responses: Dict[int, float] = {}
        self._next_id = 0
        self._latest_arrival = threading.Event()
        self._latest_ts: float | None = None

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"airbench-z2m-{int(time.time()*1000)}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect(host, port, 30)
        self.client.loop_start()

    def _on_connect(self, c, u, f, rc, properties=None):
        c.subscribe(self.resp_topic)

    def _on_message(self, c, u, msg):
        if msg.topic != self.resp_topic:
            return
        ts = time.monotonic()
        with self._lock:
            self._latest_ts = ts
            self._latest_arrival.set()

    def _publish_and_wait(self, topic: str, payload: str,
                          timeout: float) -> float | None:
        with self._lock:
            self._latest_arrival.clear()
            self._latest_ts = None
        t0 = time.monotonic()
        self.client.publish(topic, payload, qos=0)
        if self._latest_arrival.wait(timeout):
            with self._lock:
                if self._latest_ts is None:
                    return None
                return (self._latest_ts - t0) * 1000.0
        return None

    def read_basic_attr(self, attr: int = 0,
                         timeout: float = 5.0) -> float | None:
        # attribute 0 = zclVersion (1 byte). Direct ZCL read goes via /set.
        return self._publish_and_wait(self.set_topic,
                                        json.dumps({"read": {
                                            "cluster": "genBasic",
                                            "attributes": [attr],
                                        }}),
                                        timeout)

    def read_n_attrs(self, n: int, timeout: float = 5.0) -> float | None:
        # Reading N attributes from genBasic.
        # Request bytes ≈ 3 (ZCL header) + 2 * n (attribute IDs).
        # Response includes status + value per attribute.
        return self._publish_and_wait(self.set_topic,
                                        json.dumps({"read": {
                                            "cluster": "genBasic",
                                            "attributes": list(range(n)),
                                        }}),
                                        timeout)

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def sweep_rate(probe: Probe, rate_msgs_s: float, count: int) -> Dict[str, Any]:
    spacing = 1.0 / rate_msgs_s if rate_msgs_s > 0 else 0
    lats: List[float] = []
    losses = 0
    next_deadline = time.monotonic()
    for _ in range(count):
        now = time.monotonic()
        if now < next_deadline:
            time.sleep(next_deadline - now)
        rtt = probe.read_basic_attr("zclVersion", timeout=2.0)
        if rtt is None:
            losses += 1
        else:
            lats.append(rtt)
        next_deadline += spacing
    return {
        "rate_msgs_s": rate_msgs_s,
        "sent": count,
        "received": count - losses,
        "loss_pct": round(losses / count * 100, 2),
        "rtt_ms_p50": round(statistics.median(lats), 2) if lats else None,
        "rtt_ms_p95": (round(statistics.quantiles(lats, n=20)[18], 2)
                        if len(lats) >= 20
                        else round(max(lats), 2) if lats else None),
        "rtt_ms_max": round(max(lats), 2) if lats else None,
        "rtt_ms_min": round(min(lats), 2) if lats else None,
    }


def sweep_size(probe: Probe, n_attrs_list: List[int], count_per_size: int,
                spacing_ms: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for n in n_attrs_list:
        request_bytes = 3 + 2 * n
        lats: List[float] = []
        losses = 0
        for _ in range(count_per_size):
            rtt = probe.read_n_attrs(n, timeout=3.0)
            if rtt is None:
                losses += 1
            else:
                lats.append(rtt)
            time.sleep(spacing_ms / 1000.0)
        rows.append({
            "n_attrs": n,
            "request_bytes": request_bytes,
            "sent": count_per_size,
            "received": count_per_size - losses,
            "loss_pct": round(losses / count_per_size * 100, 2),
            "rtt_ms_p50": round(statistics.median(lats), 2) if lats else None,
            "rtt_ms_p95": (round(statistics.quantiles(lats, n=20)[18], 2)
                            if len(lats) >= 20
                            else round(max(lats), 2) if lats else None),
            "rtt_ms_max": round(max(lats), 2) if lats else None,
            "throughput_bps": (round(len(lats) * request_bytes * 8
                                      / (count_per_size * spacing_ms / 1000.0), 1)
                                if lats else 0.0),
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", type=int, default=11883)
    ap.add_argument("--base", default="zigbee")
    ap.add_argument("--device", required=True,
                    help="z2m friendly_name of the router (IEEE address)")
    ap.add_argument("--rates", default="1,5,10,20,50,100",
                    help="messages per second for the rate sweep")
    ap.add_argument("--rate-count", type=int, default=50)
    ap.add_argument("--sizes", default="1,2,4,8,12",
                    help="number of attributes per read (request size = 3 + 2*N bytes)")
    ap.add_argument("--size-count", type=int, default=20)
    ap.add_argument("--size-spacing-ms", type=int, default=200)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.out is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        args.out = SCRIPT_DIR / "runs" / f"airbench-z2m-{ts}.json"
    args.out.parent.mkdir(parents=True, exist_ok=True)

    rates = [float(x) for x in args.rates.split(",")]
    sizes = [int(x) for x in args.sizes.split(",")]

    probe = Probe(args.mqtt_host, args.mqtt_port, args.base, args.device)
    time.sleep(0.5)

    print(f"=== rate sweep (genBasic.zclVersion read, count={args.rate_count}) ===",
          flush=True)
    rate_rows: List[Dict[str, Any]] = []
    for rate in rates:
        row = sweep_rate(probe, rate, args.rate_count)
        rate_rows.append(row)
        print(f"  {rate:>6}/s sent={row['sent']} recv={row['received']} "
              f"loss={row['loss_pct']}% p50={row['rtt_ms_p50']}ms "
              f"p95={row['rtt_ms_p95']}ms max={row['rtt_ms_max']}ms",
              flush=True)
        time.sleep(2.0)

    print(f"\n=== size sweep (genBasic multi-attr read, "
          f"count={args.size_count}, spacing={args.size_spacing_ms}ms) ===",
          flush=True)
    size_rows = sweep_size(probe, sizes, args.size_count, args.size_spacing_ms)
    for row in size_rows:
        print(f"  N={row['n_attrs']} ({row['request_bytes']}B) sent={row['sent']} "
              f"recv={row['received']} loss={row['loss_pct']}% "
              f"p50={row['rtt_ms_p50']}ms max={row['rtt_ms_max']}ms "
              f"thr={row['throughput_bps']}bps", flush=True)

    probe.stop()

    args.out.write_text(json.dumps({
        "device": args.device,
        "rate_sweep": rate_rows,
        "size_sweep": size_rows,
    }, indent=2))
    print(f"\nwrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
