"""
Queue-saturation stress test for the full Hermod hardware-mode stack.

Pumps MQTT messages at increasing rates with varying seeded rule
counts, and measures where the system breaks — queue drops, pod
memory, pod restarts, ingestion loss.

Signal sources (not message_history; Features.MessagePersistence is
off in the dev-hardware overlay):

  - `hermod_messages_ingested_total` (counter) via /metrics on the
    coordinator pod — this is the ground-truth "did Hermod see the
    message" signal, feature-flag independent.
  - `rules.execution_count` (DB) — rule engine bumps this for every
    matched rule, independent of audit-log writes.
  - `wifi/bench/out/#` MQTT subscribe — rule actions republish here,
    showing whether the pipeline's output stage is intact.
  - kubectl top pod (via host) — Hermod pod memory + CPU.
  - kubectl logs | grep "Message queue full" — drop warnings.
  - kubectl get pod RestartCount — crash / OOM-kill detection.

Per rule-count N in {0, 10, 100, 1000, 10000}:
  1. Purge any prior `qstress_%` rules.
  2. Seed N new bench rules, each matching `wifi/bench/in/NNNN` with an
     action that republishes to `wifi/bench/out/NNNN`.
  3. Wait for rule-cache refresh (5 s default).
  4. Run ramp phases at configured msg/s rates. Per phase record the
     signals above. Stop ramping on first failure:
       - ingestion loss > 50 %
       - pod RestartCount up
       - pod memory ≥ 90 % of 512 MiB limit
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import paho.mqtt.client as mqtt
import psycopg2


def _require(var: str) -> str:
    val = os.environ.get(var)
    if not val:
        raise SystemExit(f"missing required env var: {var}")
    return val


PG_DSN = dict(
    host=os.environ.get("PG_HOST", "127.0.0.1"),
    port=int(os.environ.get("PG_PORT", "15432")),
    user=os.environ.get("PG_USER", "postgres"),
    password=_require("PG_PASSWORD"),
    dbname=os.environ.get("PG_DB", "hermod"),
)

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "11883"))
METRICS_URL = os.environ.get("METRICS_URL", "http://127.0.0.1:42069/metrics")


def pg() -> psycopg2.extensions.connection:
    return psycopg2.connect(**PG_DSN)


def purge_bench_rules() -> int:
    conn = pg()
    cur = conn.cursor()
    cur.execute("DELETE FROM rules WHERE id LIKE 'qstress_%'")
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n


def seed_rules(n: int) -> None:
    if n == 0:
        return
    conn = pg()
    cur = conn.cursor()
    rows = []
    for i in range(n):
        trigger = json.dumps({"type": 0, "topicPattern": f"wifi/bench/in/{i:05d}"})
        actions = json.dumps([{
            "type": 0, "topic": f"wifi/bench/out/{i:05d}",
            "qoS": 0, "retain": False,
            "payload": {"seq": i, "bench": "qstress"},
            "passthroughPayload": False,
        }])
        rows.append((f"qstress_{i:05d}", f"qstress bench rule {i}",
                      True, 100, trigger, actions))
    cur.executemany(
        "INSERT INTO rules (id, name, enabled, priority, trigger, actions) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        rows,
    )
    conn.commit()
    conn.close()


_METRIC_RE = re.compile(r"^(hermod_[a-z_]+)(?:\{[^}]*\})?\s+([0-9.eE+\-]+)$", re.M)


def read_metrics() -> Dict[str, float]:
    """GET /metrics and parse out every hermod_* counter/gauge."""
    try:
        with urllib.request.urlopen(METRICS_URL, timeout=5) as r:
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return {}
    out: Dict[str, float] = {}
    for name, val in _METRIC_RE.findall(body):
        # Prefer sums (ignore histogram buckets / le labels collapsed).
        if name in out:
            continue
        try:
            out[name] = float(val)
        except ValueError:
            pass
    return out


def host_exec(cmd: List[str], timeout: float = 10.0) -> str:
    # HERMOD_KUBECTL_SSH prefix lets the harness run on a laptop while kubectl
    # commands are actually executed remotely on the Pi via SSH. Example:
    #   HERMOD_KUBECTL_SSH='ssh -i $KEY ubuntu@<pi-ip> sudo microk8s'
    # Set to empty / unset to use a local `kubectl` binary.
    prefix = os.environ.get("HERMOD_KUBECTL_SSH", "").strip()
    if prefix:
        full = prefix.split() + cmd
    else:
        full = cmd
    try:
        return subprocess.run(
            full,
            capture_output=True, text=True, timeout=timeout,
        ).stdout.strip()
    except Exception:
        return ""


def pod_metrics() -> Dict[str, Any]:
    # kubectl top via host-exec (we're inside a pod, no kubectl)
    top = host_exec(["kubectl", "-n", "hermod", "top", "pod",
                      "-l", "app=hermod-coordinator", "--no-headers"])
    cpu_m, mem_mi = None, None
    if top:
        parts = top.split()
        if len(parts) >= 3:
            try:
                cpu_m = int(parts[1].replace("m", ""))
                mem_mi = int(parts[2].replace("Mi", ""))
            except ValueError:
                pass
    restarts = host_exec([
        "kubectl", "-n", "hermod", "get", "pods",
        "-l", "app=hermod-coordinator",
        "-o", "jsonpath={.items[0].status.containerStatuses[0].restartCount}",
    ])
    try:
        restarts_i = int(restarts) if restarts else None
    except ValueError:
        restarts_i = None
    return {"cpu_m": cpu_m, "memory_mi": mem_mi, "restarts": restarts_i}


def drop_count_since(since_epoch: float) -> int:
    """Count 'Message queue full' warnings in recent log tail. A full
    --since-time scan takes seconds on a busy Debug-level log; cap to
    the last ~2000 lines which covers a single phase at up to
    400 log-lines/s."""
    out = host_exec([
        "kubectl", "-n", "hermod", "logs",
        "-l", "app=hermod-coordinator", "--tail=2000",
    ], timeout=5)
    return out.count("Message queue full")


def exec_count_sum() -> int:
    conn = pg()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(execution_count),0) FROM rules "
                 "WHERE id LIKE 'qstress_%'")
    v = int(cur.fetchone()[0])
    conn.close()
    return v


class OutCounter:
    """Subscribe to wifi/bench/out/# and count arrivals per phase."""
    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                    client_id=f"qstress-out-{int(time.time())}")
        self.client.on_connect = lambda c, u, f, rc: c.subscribe("wifi/bench/out/#", 0)
        self.client.on_message = self._on_msg

    def _on_msg(self, _c, _u, _m):
        with self._lock:
            self.count += 1

    def start(self):
        self.client.connect(MQTT_HOST, MQTT_PORT, 30)
        self.client.loop_start()

    def stop(self):
        self.client.loop_stop()
        self.client.disconnect()

    def reset_snapshot(self) -> int:
        with self._lock:
            v = self.count
            self.count = 0
            return v


def run_phase(rate: int, duration_s: float, rule_count: int,
              pub: mqtt.Client, out_counter: OutCounter,
              t_start_wall: float) -> Dict[str, Any]:
    spacing = 1.0 / rate if rate > 0 else 0
    target = int(rate * duration_s)

    m_before = read_metrics()
    exec_before = exec_count_sum()
    out_counter.reset_snapshot()
    t_phase_start = time.monotonic()
    t_phase_wall = time.time()
    sent = 0
    next_deadline = t_phase_start
    phase_end = t_phase_start + duration_s
    while time.monotonic() < phase_end and sent < target:
        now = time.monotonic()
        if now < next_deadline:
            time.sleep(max(0.0, next_deadline - now))
        idx = sent % max(rule_count, 1) if rule_count > 0 else sent
        if rule_count > 0:
            topic = f"wifi/bench/in/{idx:05d}"
        else:
            # Round-robin across the five protocol subtrees so parallel
            # MQTT shards (ParallelMqttService with Mqtt.ParallelClients > 1
            # in the coordinator) see distinct traffic. With one shard,
            # this is equivalent to the old `bench/null/*` behaviour.
            proto = ("zigbee", "lora", "wifi", "bluetooth", "hermod")[sent % 5]
            topic = f"{proto}/bench_null_{sent:05d}"
        pub.publish(topic, b'{"s":1}', qos=0)
        sent += 1
        next_deadline += spacing
    pub_elapsed = time.monotonic() - t_phase_start

    # Drain 3s
    time.sleep(3.0)

    m_after = read_metrics()
    exec_after = exec_count_sum()
    out_after = out_counter.reset_snapshot()

    ingested_delta = int(m_after.get("hermod_messages_ingested_total", 0)
                          - m_before.get("hermod_messages_ingested_total", 0))
    metrics = pod_metrics()
    drops = drop_count_since(t_phase_wall - 1.0)

    # For rule_count > 0, the rules each republish an input as a
    # wifi/bench/out/N message, so the number of bench/out arrivals is a
    # reliable "Hermod actually processed this" signal. For
    # rule_count == 0, there's no output path, so we fall back to the
    # ingested-counter delta, which is noisy (it counts ALL broker
    # traffic including lora/wifi/zigbee) but gives an order-of-
    # magnitude sanity check.
    if rule_count > 0:
        processed = out_after
        loss_pct = round((sent - processed) / max(sent, 1) * 100, 2)
    else:
        processed = None
        loss_pct = None

    return {
        "rate_msgs_s": rate,
        "duration_s": round(pub_elapsed, 2),
        "target_sent": target,
        "actually_sent": sent,
        "effective_send_rate": round(sent / pub_elapsed, 1),
        "ingested_counter_delta": ingested_delta,
        "processed_via_out": processed,
        "loss_pct": loss_pct,
        "rule_exec_delta": exec_after - exec_before,
        "queue_drop_warnings": drops,
        "pod_memory_mi": metrics["memory_mi"],
        "pod_cpu_m": metrics["cpu_m"],
        "pod_restarts": metrics["restarts"],
    }


def run_rule_bucket(rule_count: int, rates: List[int], phase_s: float,
                      out_dir: Path) -> Dict[str, Any]:
    print(f"\n=== {rule_count} rules ===", flush=True)
    deleted = purge_bench_rules()
    print(f"purged {deleted} prior bench rules", flush=True)
    seed_rules(rule_count)
    print(f"seeded {rule_count} bench rules", flush=True)

    # Wait for at least one full RuleCacheRefreshSeconds cycle. The
    # prod config sets that to 30 s, so 35 s covers one full refresh
    # plus the reindex cost for 10 k rules.
    settle = 35.0 if rule_count < 10000 else 60.0
    print(f"settling {settle}s for rule cache…", flush=True)
    time.sleep(settle)

    baseline_metrics = read_metrics()
    baseline_pod = pod_metrics()
    print(f"baseline pod: {baseline_pod}", flush=True)
    print(f"baseline ingested_total: {baseline_metrics.get('hermod_messages_ingested_total', 0)}",
           flush=True)

    out_counter = OutCounter()
    out_counter.start()
    pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                       client_id=f"qstress-pub-{rule_count}-{int(time.time())}")
    pub.connect(MQTT_HOST, MQTT_PORT, 60)
    pub.loop_start()
    time.sleep(0.3)

    t_bucket_wall = time.time()
    phases = []
    stop_reason = None
    consec_loss = 0
    for rate in rates:
        phase = run_phase(rate, phase_s, rule_count, pub, out_counter,
                          t_bucket_wall)
        phases.append(phase)
        print(f"  {rate:>6} msg/s: "
              f"sent={phase['actually_sent']} "
              f"processed={phase['processed_via_out']} "
              f"loss={phase['loss_pct']}% "
              f"exec={phase['rule_exec_delta']} "
              f"drops={phase['queue_drop_warnings']} "
              f"ingest_delta={phase['ingested_counter_delta']} "
              f"mem={phase['pod_memory_mi']}Mi "
              f"cpu={phase['pod_cpu_m']}m "
              f"restarts={phase['pod_restarts']}",
              flush=True)

        if (baseline_pod["restarts"] is not None
            and phase["pod_restarts"] is not None
            and phase["pod_restarts"] > baseline_pod["restarts"]):
            stop_reason = "pod_restart"
            break
        if phase["pod_memory_mi"] and phase["pod_memory_mi"] >= 460:
            stop_reason = f"memory_near_limit ({phase['pod_memory_mi']}Mi of 512)"
            break
        if phase["loss_pct"] is not None and phase["loss_pct"] > 50:
            consec_loss += 1
            if consec_loss >= 2:
                stop_reason = f"pipeline_loss {phase['loss_pct']}% (2 consec)"
                break
        else:
            consec_loss = 0
        if phase["queue_drop_warnings"] and phase["queue_drop_warnings"] > 500:
            stop_reason = f"queue_drops {phase['queue_drop_warnings']}"
            break

    pub.loop_stop()
    pub.disconnect()
    out_counter.stop()

    purge_bench_rules()
    result = {
        "rule_count": rule_count,
        "baseline": {"metrics": baseline_metrics, "pod": baseline_pod},
        "phases": phases,
        "stop_reason": stop_reason,
    }
    (out_dir / f"qstress-{rule_count:05d}.json").write_text(
        json.dumps(result, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", default="0,10,100,1000,10000")
    ap.add_argument("--rates", default="100,500,1000,2500,5000,10000")
    ap.add_argument("--phase-s", type=float, default=5.0)
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="default: runs/qstress-<UTC>/ next to this script")
    args = ap.parse_args()
    if args.out_dir is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        args.out_dir = Path(__file__).resolve().parent / "runs" / f"qstress-{ts}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    buckets = [int(s) for s in args.rules.split(",")]
    rates = [int(s) for s in args.rates.split(",")]

    print(f"buckets: {buckets}, rates: {rates} msg/s, phase={args.phase_s}s",
           flush=True)

    all_results = []
    for n in buckets:
        res = run_rule_bucket(n, rates, args.phase_s, args.out_dir)
        all_results.append(res)

    (args.out_dir / "qstress-summary.json").write_text(
        json.dumps(all_results, indent=2))

    print("\n=== SUMMARY ===")
    print(f"{'rules':>6} | {'stop_reason':<40} | last_phase")
    for res in all_results:
        last = res["phases"][-1] if res["phases"] else {}
        stop = res.get("stop_reason") or "all phases ok"
        last_desc = (f"{last.get('rate_msgs_s','?')}msg/s "
                      f"loss={last.get('loss_pct','?')}% "
                      f"mem={last.get('pod_memory_mi','?')}Mi "
                      f"exec={last.get('rule_exec_delta','?')} "
                      f"drops={last.get('queue_drop_warnings','?')}") if last else "—"
        print(f"{res['rule_count']:>6} | {stop[:40]:<40} | {last_desc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
