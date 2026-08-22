# ZigBee bench

Adapter-level, bridge-level and full-system measurements for the
ZigBee side of Hermod. Two Sonoff ZBDongle-E Plus V2 dongles on the
same desk drive each other over-the-air on channel 15; one runs the
production EmberZNet 8.0.3 NCP firmware and is driven by
`zigbee2mqtt`, the other runs the Itead Z3 Router firmware
(EmberZNet 6.10.3) and joins as a router. Bench code lives in
`zb-bench/`; full-system results from the campaign's F4 / J-bg-zigbee
phases live under `raw/<ts>-…F4-zb-bench/` and `raw/<ts>-…J-bg-zigbee/`.

## Hardware

| Slot | Firmware | EZSP version | Role |
|---|---|---|---|
| `/dev/ttyUSB0` | EmberZNet 8.0.3 GA (`darkxst` 8.x build) | 16 | Coordinator (NCP) |
| `/dev/ttyUSB1` | Itead Z3 Router (EmberZNet 6.10.3 GA) | — | Router on coordinator's PAN |

Both dongles are EFR32MG21 + CP210x USB bridges. The `darkxst/silabs-firmware-builder`
8.0.3 GBL was flashed with `universal-silabs-flasher --bootloader-reset rts_dtr`
(no BOOT button on this hardware rev — RTS/DTR drives the bootloader internally).
Flash recipe preserved at the bottom of this file.

| Parameter | Value |
|---|---|
| Module | Sonoff ZBDongle-E Plus V2 |
| Radio SoC | Silicon Labs EFR32MG21 |
| USB bridge | SiLabs CP210x (VID 10c4 / PID ea60) |
| Host UART baud | 115 200 8N1, software flow control |
| PHY | IEEE 802.15.4 at 2.4 GHz |
| Chip rate | 250 kbit/s O-QPSK |
| Channels | 11 – 26 (2 405 – 2 480 MHz); bench used **15** (2 425 MHz) |
| TX power (EmberZNet default) | +8 dBm (≈ 6 mW) |
| RX sensitivity | −100 dBm (EFR32MG21 datasheet) |

## ASH link layer (raw USB ↔ NCP)

`ash_probe.py` drives each dongle at the ASH-v2 layer directly
(bypassing bellows / zigpy) and measures cold-boot RSTACK plus EZSP
round-trip:

| Metric | ttyUSB0 | ttyUSB1 |
|---|---:|---:|
| External-reset → RSTACK | **1 153.68 ms** | **1 153.92 ms** |
| EZSP round-trip p50 | **53.13 ms** | **53.05 ms** |
| EZSP round-trip max | 53.35 ms | 53.14 ms |

53 ms is the floor latency for any operation that crosses the USB-ASH
boundary; firmware version doesn't move it (CP210x USB scheduling +
NCP-side ASH ACK generation is the bottleneck, not the EmberZNet
stack). **Operational note:** if a previous bellows / zigpy session
closed the port abruptly, the NCP stays mid-DATA and subsequent ASH
RSTs are ignored. Toggling DTR + RTS (off → on → off) from pyserial
brings it back. Bellows 0.49.0 does not do this pulse and therefore
does not work reliably on these dongles.

## zigbee2mqtt cold start

`z2m_bench.py` observes MQTT from subscribe-before-launch, times from
`podman run -d` to `bridge/state = {"state":"online"}`, and records
the seven bridge topics it sees:

| Run | Cold start | Topic fanout | Notes |
|---|---:|---:|---|
| ttyUSB0 (NCP firmware EmberZNet 8.0.3, single dongle) | **3.92 s** | **0.02 s** | most recent run, 2026-04-30 |
| ttyUSB1 (Router firmware Itead Z3, single dongle) | 3.97 s | 0.02 s | same |

Bridge topics on subscribe (sizes for reference):

| Topic | Size | Retention | Role |
|---|---:|---|---|
| `zigbee/bridge/state` | 18 B | retained | `{"state":"online"}` |
| `zigbee/bridge/info` (initial) | ~26.8 KB | retained | full coordinator + config dump |
| `zigbee/bridge/info` (steady) | 80 B | retained | `{"version","coordinator","network"}` |
| `zigbee/bridge/definitions` | **230.9 KB** | retained | all supported device models |
| `zigbee/bridge/devices` | 857 B | retained | coordinator-only list |
| `zigbee/bridge/converters` | 2 B | retained | `[]` |
| `zigbee/bridge/extensions` | 2 B | retained | `[]` |
| `zigbee/bridge/groups` | 2 B | retained | `[]` |

Practical implication: any MQTT client subscribing to `zigbee/bridge/#`
on a cold broker immediately pulls 231 KB of retained `definitions`.
Hermod's `MessageProcessor` consumes this once per boot and parses it
as JSON — measurable but not pathological one-shot cost.

## Permit-join command round-trip

Send `zigbee/bridge/request/permit_join` with `{"value":true,"time":5}`,
wait for `zigbee/bridge/response/permit_join`. Full path: MQTT → broker
→ z2m → zigbee-herdsman → ember adapter → NCP via ASH → response back.

| Segment | Latency | Source |
|---|---:|---|
| Broker → z2m subscriber wake | 4 ms | z2m log `"allowing new devices to join"` at +4 ms |
| z2m → NCP → stack-status callback | 25 ms | z2m log `"[STACK STATUS] Network opened"` at +29 ms |
| Stack callback → MQTT response publish | 521 ms | z2m holds the promise open for permit-state confirmation |
| **Total request → response round-trip** | **~554 ms** | both dongles within ± 5 ms across coexistence configs |

The Zigbee-side command-latency ceiling is dominated by the 521 ms
confirmation-wait inside z2m's `PermitJoin` promise, not by MQTT
transport or USB-ASH. Optimisation above ~550 ms must live inside
z2m (or replace it).

## Two-dongle coexistence

`coexist_bench.py` runs both z2m instances in parallel on different
MQTT base topics (`zigbee` + `zigbee2`) and different channels, then
fires permit_join on each. Three configurations:

| Configuration | tty0 cold start | tty1 cold start | tty0 permit | tty1 permit | offline events |
|---|---:|---:|---:|---:|---:|
| ch 15 + ch 20 (distant) | 3.79 s | 3.88 s | 553.76 ms | 557.70 ms | 0 / 0 |
| ch 15 + ch 16 (adjacent) | 3.78 s | 3.94 s | 552.57 ms | 554.97 ms | 0 / 0 |
| **ch 15 + ch 15 (same)** | 3.79 s | 3.91 s | 552.21 ms | 554.67 ms | 0 / 0 |

At idle, two coordinators on the **same channel** produced identical
latency to coordinators on distant channels. Both form independent
PANs (distinct PAN IDs) and the shared host / broker handles them
without spurious `state=offline` events. **This is not** a claim that
channel separation is unnecessary for real mesh operation — it's a
claim that the coordinator-layer command path (MQTT → dongle NCP) is
insensitive to channel choice in an otherwise-idle setting.

The concurrent-boot penalty on tty0 is ~0.9 s vs standalone (3.79 s
vs 2.90 s in the historical run). Same ballpark cause as the LoRa
bench: container-image cold cache plus shared IO bandwidth during
simultaneous `podman run`.

## Real over-the-air command behaviour (ZCL via z2m)

The most recent run (`airbench-z2m-20260430T180503Z.json`) measured
real OTA command behaviour by reading attributes on the joined router
via zigbee2mqtt's direct ZCL endpoint. Two sweeps; **the two numbers
are not directly comparable** — they measure different operating
regimes.

### Scheduled-rate sweep (one-byte `genBasic.zclVersion` reads)

This harness waits for each response before submitting the next command,
so the high rate labels below are scheduled inter-request targets, not
achieved pipelined throughput.

| Scheduled rate | Sent | Loss | RTT p50 | RTT p95 |
|---:|---:|---:|---:|---:|
| 1 msg/s | 50 | 0 % | 40.02 ms | 42.51 ms |
| 2 msg/s | 50 | 0 % | 39.92 ms | 87.81 ms |
| 5 msg/s | 50 | 0 % | 40.07 ms | 42.32 ms |
| 10 msg/s | 50 | 0 % | 40.30 ms | 41.72 ms |
| 20 msg/s | 50 | 0 % | 54.30 ms | 80.77 ms |
| 50 msg/s | 50 | 0 % | 53.62 ms | 60.23 ms |
| 100 msg/s | 50 | 0 % | 53.48 ms | 59.50 ms |
| 200 msg/s | 50 | 0 % | 54.31 ms | 81.04 ms |
| 500 msg/s | 50 | 0 % | 54.09 ms | 56.85 ms |
| 1 000 msg/s | 50 | 0 % | 53.98 ms | 70.35 ms |
| **2 000 msg/s** | 50 | **0 %** | 53.35 ms | 70.33 ms |

Median RTT stays at ~40 ms up to 10 msg/s and rises to ~54 ms at
the compressed scheduling points. The achieved command cadence is
therefore bounded by the request-response round-trip, not by the
scheduled-rate label. **Loss is zero at every scheduled point up to
2 000 msg/s.**

### Size sweep (multi-attribute reads, sequential)

Each additional attribute identifier adds 2 bytes to the ZCL request,
so request size scales linearly from 5 B at `N = 1` to 23 B at `N = 10`:

| N | Request | Sent | Loss | RTT p50 | App-throughput |
|---:|---:|---:|---:|---:|---:|
| 1 | 5 B | 30 | 0 % | 40.08 ms | 200 bps |
| 2 | 7 B | 30 | 0 % | 44.32 ms | 280 bps |
| 3 | 9 B | 30 | 0 % | 46.62 ms | 360 bps |
| 4 | 11 B | 30 | 0 % | 48.51 ms | 440 bps |
| 5 | 13 B | 30 | 0 % | 52.06 ms | 520 bps |
| 6 | 15 B | 30 | 0 % | 56.04 ms | 600 bps |
| 7 | 17 B | 30 | 0 % | 59.22 ms | 680 bps |
| 8 | 19 B | 30 | 0 % | 61.05 ms | 760 bps |
| 9 | 21 B | 30 | 0 % | 63.21 ms | 840 bps |
| 10 | 23 B | 30 | 0 % | 65.60 ms | 920 bps |

Sweep stopped at `N = 11` because the zigbee-herdsman frame builder
rejects the request as exceeding its maximum payload size — this is
a software limit (the ZCL builder), not a radio limit.

### Why the gap to chip rate

Single-request application throughput remains approximately two orders of magnitude
below the 802.15.4 O-QPSK chip rate of 250 kbit/s and three orders
below the Shannon-Hartley capacity for the same 2 MHz channel at the
EFR32MG21 sensitivity SNR (≈ 1 Mbps at −3 dB, derived from the
−100 dBm sensitivity datasheet figure at 1 % PER). Three contributions:

1. Each ZCL read carries framing overhead at four protocol layers
   (ZCL header, APS header, network-layer header, 802.15.4 MAC
   header). On-air bytes per request are ~4–5× the application
   payload, and each request elicits a response of equivalent size.
2. The USB-ASH transport imposes the ~53 ms median round-trip floor
   characterised above; without a genuinely pipelined command window,
   the link sits idle for most of each transaction.
3. The size sweep measures sequential single-request round-trips at
   minimum spacing.

The scheduled-rate sweep shows loss-free request handling across the
attempted spacing range, but it does not demonstrate 2 000 achieved
commands per second. The binding constraint at the measured single-shot
throughput is **request-response cadence**, not over-the-air bandwidth.

## Hermod end-to-end ingestion (historical, via z2m bench)

`hermod_latency_probe.py` publishes synthetic messages to
`zigbee/bench_<marker>_NNNN` topics, then queries Postgres
`message_history.received_at` to measure publish → Hermod ingestion.
Sweep at increasing rates:

| Spacing | ≈ Rate | Loss | p50 | p95 | Max |
|---:|---:|---:|---:|---:|---:|
| 100 ms | 10 msg/s | 0 % | 210 ms | 503 ms | 504 ms |
| 50 ms | 20 msg/s | 0 % | 258 ms | 502 ms | 510 ms |
| 25 ms | 40 msg/s | 0 % | 158 ms | 385 ms | 487 ms |
| 10 ms | 100 msg/s | 0 % | 58 ms | 119 ms | 510 ms |
| 5 ms | 200 msg/s | 0 % | 36 ms | 86 ms | 504 ms |
| 2 ms | 500 msg/s | 0 % | 545 ms | 970 ms | 1 034 ms |
| 1 ms | 1 000 msg/s | 0 % | 842 ms | 1 446 ms | 1 497 ms |
| burst | ~84 000 msg/s | **73 %** | — | — | — |

The ~500 ms ceiling at low rates is Hermod's `message_history`
batch-commit window: the Postgres writer flushes its channel every
~500 ms, so a single message at low arrival waits out the residual
of that window on average. At higher rates the batch fills faster
and p50 drops below the commit-window tail — which is why p50 gets
**better** as rate climbs from 10 msg/s (210 ms) to 200 msg/s (36 ms).

**Sustained 1 000 msg/s ingested with zero loss.** A 100-device
ZigBee home network reporting at 1 Hz aggregates to 100 msg/s — ten
times below this ceiling. The 84 000 msg/s burst saturates both
bounded queues (`MessageProcessor._messageQueue` 10 000 / `DropOldest`,
`PostgresMessageHistoryRepository._queue` 50 000 / `DropOldest`)
and drops 73 %. Failure mode is silent at the MQTT layer — Hermod
logs each drop with `"Message queue full, dropping message on topic …"`
at level Warning; the broker and publisher see 100 % publish success.

## Pi5 real-hardware run (2026-04-19, snapshot)

The first end-to-end run of Hermod on actual Pi 5 hardware (not kind,
not QEMU). Captured separately from the campaign in
`zb-bench/results-pi5-real-20260419T2319/`. Reproduces the campaign's
broad shape; included here for the historical "is this thing actually
running on a Pi" milestone.

| Layer | Value |
|---|---|
| Board | Raspberry Pi 5, 8 GB |
| OS | Ubuntu Server 24.04.4 LTS arm64 |
| Orchestrator | MicroK8s 1.30/stable (snap) |
| Coordinator image | self-built, arm64 |
| Broker | NanoMQ 0.21-slim |
| Storage | Postgres 17-alpine |

Headline numbers from the run:

| Metric | Value |
|---|---|
| Raw ingest ceiling, 0 rules | **~8 000 msg/s** (CPU-bound, 1-core saturation) |
| Loss-free with 10 rules (fresh coord) | **500 msg/s** |
| Loss-free with 100 rules (fresh coord) | **500 msg/s** |
| Coordinator RAM footprint | 86 – 170 MiB (grows with rule cache + bench traffic) |
| Coordinator CPU at idle (post-burst) | 900 – 960 m residual (see "Known issues" below) |

**Known issue from this run:** rule-cache degrades after high-volume
bursts. After a 0-rule ceiling probe pushing 8k msg/s for 30 s+, the
coord pod sits at ~950 m CPU at idle (zero ambient messages in
`ingested_total`), and subsequent rule-match tests see `exec=0` at
rates that were fine on a fresh pod. Restarting the coord pod clears
it. Worth investigating the rule-cache refresh path under contention.

## Resilience + watchdog (kind cluster)

Recovery times measured during pre-campaign hardening on kind:

| Failure | Detection | Recovery | Restart required |
|---|---|---|---|
| Coordinator OOM-kill | liveness probe ≤ 40 s | pod restart, ~11 s | yes (automatic) |
| Coordinator HTTP hang | liveness probe ≤ 40 s | pod restart, ~11 s | yes |
| Coordinator "healthy but silent" | **watchdog ping/pong, ~120 s** | rollout restart, ~15 s | yes (watchdog-triggered) |
| NanoMQ pod crash | rollout, ~11 s | self (broker), watchdog MISS for ≤ 2 min then coord recovers | no for Hermod alone |
| Postgres pod crash | (no k8s alert; silent writes-dropping under DropOldest) | new pg pod up in ≤ 30 s; coord retries on next batch | no |
| Rule cache stale (new rules not reloading) | — | `RuleCacheRefreshSeconds` cycle | no |

The pipeline-level watchdog (`pipeline-watchdog.yaml`) closes the
"healthy but silent" gap that standard Kubernetes liveness probes
miss: it publishes `hermod/watchdog/ping <seq>` every 30 s, expects
the matching pong via a `watchdog_echo` rule that passthrough-publishes,
and on 3 consecutive misses calls `kubectl rollout restart
deployment/hermod-coordinator` then sleeps 90 s to avoid storm-restart.

End-to-end recovery from a NanoMQ kill: **3 m 58 s** worst case with
`INTERVAL_S=30` + `MISS_LIMIT=3`. Tunable to ~1 m 30 s at the cost
of 2× the canary traffic.

The bug-ish behaviour the watchdog protects against: Hermod's
`ReconnectLoopAsync` logs warnings on each retry but on a clean
`NormalDisconnection` (the code path when the broker server-side
disappears and comes back) the resubscribe after reconnect is not
guaranteed.

## Hard-limit summary

| Metric | Value | Source |
|---|---|---|
| PHY chip rate | 250 kbit/s | 802.15.4 datasheet |
| USB↔NCP ASH round-trip floor | **53 ms** | `ash_probe.py`, both dongles |
| NCP cold-boot (external reset) | **1 154 ms** | `ash_probe.py`, both dongles |
| z2m cold start | **3.92 – 3.97 s** | `z2m_bench.py`, recent run |
| Topic fanout after `state=online` | **20 ms** | recent `z2m_bench.py` |
| Retained `definitions` blob | **230.9 KB** | `z2m_bench.py` |
| MQTT command round-trip (permit_join) | **~554 ms** | `z2m_bench.py` + coexist |
| z2m → NCP one-way latency | **29 ms** | z2m log timestamps |
| Two-coord same-channel penalty (idle) | **≈ 0 ms** | `coexist_bench.py`, idle |
| Scheduled-rate loss check | **0 % loss** through attempted 2 000 msg/s schedule; sequential request/response harness | recent airbench-z2m |
| Sequential single-request RTT | **40 ms p50** at low rate | recent airbench-z2m |
| App-layer throughput | **200 – 920 bps** (size sweep N=1 – 10) | recent airbench-z2m |
| Hermod sustained zero-loss (z2m bench) | **1 000 msg/s** | `hermod_latency_probe.py` |
| Hermod burst ceiling | ≥ 84 000 msg/s saturates queues | `hermod_latency_probe.py` |
| Hermod DB-commit floor | ~500 ms p50 at low rate | `message_history` batch window |

## What we cannot claim without more hardware

- **Joining end-device.** Bellows 0.49.0 does not reliably drive these
  dongles; the RSTACK handshake hangs regardless of `flow_control`.
  Building a joining-device simulator on zigbee-herdsman directly
  would work but needs a new bench harness.
- **Real ZCL payload throughput at 8 / 32 / 64 / 82 B APS-secure
  payloads, escalating rates.** Mirror of the LoRa sweep — needs at
  least one non-coordinator device on the network.
- **Device-join latency (end-to-end).** z2m's permit_join response
  (~550 ms) confirms the command was accepted; the actual join
  handshake (beacon scan, association, link-key negotiation, cluster
  discovery) runs over RF and can take 5 – 30 s depending on the
  joining device.
- **Cross-channel interference under load.** At idle, same-channel
  coexistence showed no measurable penalty. Under actual device
  traffic two coordinators on the same channel with real meshes
  behind them will collide. Needs joining devices to reproduce.
- **Long-run endurance.** Tests run for seconds-to-minutes. ASH
  counter drift, bellows-watchdog recovery, SQL connection-pool
  behaviour under hours of sustained load is out of scope.
- **RSSI / LQI per link.** Zigbee-herdsman attaches RSSI/LQI to
  received device messages; without a joined device to generate
  those messages, we cannot read them.

## Appendix — flash recipe (Itead Z3 Router, EmberZNet 6.10.3 → 8.0.3)

The router-firmware dongle was upgraded from EmberZNet 6.10.3 / EZSP 8
to the newer build for the production-firmware coordinator. There is
no BOOT button on this hardware rev; the CP210x bridge wires RTS/DTR
to BOOT/RST internally, so `universal-silabs-flasher --bootloader-reset
rts_dtr` triggers the bootloader without any button press.

```sh
podman run --rm \
  --device /dev/ttyUSB1 \
  --group-add=keep-groups \
  --entrypoint sh localhost/claude-thesis-pod:latest -c '
python3 -m venv /tmp/fl && . /tmp/fl/bin/activate
pip install --quiet universal-silabs-flasher
curl -fsSL -o /tmp/fw.gbl \
  https://github.com/darkxst/silabs-firmware-builder/releases/download/20250627/zbdonglee_zigbee_ncp_8.0.3.0_sw_flow_115200.gbl
universal-silabs-flasher --device /dev/ttyUSB1 \
  --bootloader-reset rts_dtr \
  flash --firmware /tmp/fw.gbl
'
```

Source notes: `darkxst/silabs-firmware-builder` is the only current
source for ZBDongle-E EmberZNet 8.x builds; ITEAD official ships
6.10.3, and `koenkk` / NabuCasa builders don't carry ZBDongle-E
specifically. Flash completes in ~33 s at 115 200.

## Files

| Path | Purpose |
|---|---|
| `zb-bench/ash_probe.py` | raw ASH probe (USB ↔ NCP) |
| `zb-bench/z2m_bench.py` + `run_z2m_bench.sh` | single-dongle z2m observer + orchestrator |
| `zb-bench/coexist_bench.py` + `run_coexist.sh` | dual-dongle observer + orchestrator |
| `zb-bench/hermod_latency_probe.py` | Hermod ingestion probe |
| `zb-bench/queue_stress.py` + `run_pi_stress.sh` | rule-bucket × rate stress (Hermod stack) |
| `zb-bench/airbench_z2m.py` + `bench-broker.sh` | airtime / RF throughput bench (drives the recent JSONs in `runs/`) |
| `zb-bench/runs/` | most recent airbench JSONs (rate sweep + size sweep + cold-start) |
| `zb-bench/results-pi5-real-20260419T2319/` | first Pi5 hardware milestone snapshot |
| `raw/<ts>-…F4-zb-bench/` | F4 phase output (loadgen + sampler) |
| `raw/<ts>-…J-bg-zigbee/` | J-bg-zigbee phase output (background co-residency) |
