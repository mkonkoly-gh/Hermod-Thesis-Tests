# LoRa bench

Adapter-level and bridge-level measurements for the LoRa side of
Hermod. Two identical USB LoRa adapters on the same desk drive each
other in stream mode; one is plugged into `lora2mqtt`, the other into
the bench harness. Bench code lives in `lora-bench/`; full-system
results from the campaign's F5 / J-bg-lora phases live under
`raw/<ts>-…F5-lora-bench/` and `raw/<ts>-…J-bg-lora/`.

## Hardware

| Parameter | Value | Source |
|---|---|---|
| Module | Waveshare USB-TO-LoRa-HF (SX1262-based) | USB VID:PID `1a86:55d3` (CH343 bridge) |
| Modem chip | Semtech SX1262 | module spec |
| Band | 868 MHz (EU / SRD) | adapter labelling, default AT config |
| USB bridge | WCH CH343 (CDC-ACM class) | `lsusb` |
| Host UART baud | 115 200 8N1 | `LoRaOptions.BaudRate` |
| Firmware mode | Stream / pipe (AT+MODE=1) | verified: raw bytes cross ACM0 ↔ ACM1 |

Two units are plugged in (`/dev/ttyACM0`, `/dev/ttyACM1`). AT
handshake via `+++` returned no response at any baud — the modules
are pre-configured and locked into stream mode, so there is no way
to query them at runtime. Configured radio params (the values
`WaveshareLoRaAdapter` would write if AT worked):

| Parameter | Value | Range supported by SX1262 |
|---|---|---|
| Spreading factor (SF) | 7 | 5 – 12 |
| Bandwidth | 125 kHz | 7.8 – 500 kHz |
| Coding rate | 4/5 | 4/5 – 4/8 |
| TX output power | +22 dBm (160 mW) | −9 to +22 dBm |
| Channel | 18 (868.1 MHz EU868) | — |
| Header | Explicit, CRC on, 8-symbol preamble | — |

## Adapter capability — direct serial sweep

Direct serial bench (`bench.py`), no bridge in the loop. ACM1 → ACM0:

| Payload | Frame | Spacing | Sent | Recv | Loss | p50 | Max |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 16 B | 28 B | 1000 ms | 10 | 10 | 0 % | 107 ms | 113 ms |
| 32 B | 44 B | 1000 ms | 10 | 10 | 0 % | 124 ms | 130 ms |
| 64 B | 76 B | 1000 ms | 10 | 10 | 0 % | 227 ms | 234 ms |
| 128 B | 140 B | 1000 ms | 10 | 10 | 0 % | 305 ms | 366 ms |
| 200 B | 212 B | 1000 ms | 10 | 10 | 0 % | 444 ms | 482 ms |
| 220 B | 232 B | 1500 ms | 15 | 15 | 0 % | 473 ms | 517 ms |
| **240 B** | 252 B | 1500 ms | 15 | **0** | **100 %** | — | — |
| **250 B** | 262 B | 1500 ms | 15 | **0** | **100 %** | — | — |
| **255 B** | 267 B | 1500 ms | 15 | **0** | **100 %** | — | — |

Reverse direction (ACM0 → ACM1) measured identical within ±5 ms at
every payload — the link is symmetric.

**Size ceiling:** payloads ≥ 240 B (frames ≥ 252 B) corrupt on
arrival. Bytes still reach the RX adapter but the CRC-guarded frame
parser rejects them — the SX1262 stream-mode fragmentation breaks
the frame apart. **Usable ceiling: 220 B payload / 232 B frame.**
Latency scales linearly at ≈ 2 ms per byte on-air at SF7.

### Rate ceiling at fixed 64 B

| Spacing | Sent | Recv | Loss | p50 | Note |
|---:|---:|---:|---:|---:|---|
| 500 ms | 15 | 15 | 0 % | 184 ms | clean |
| 250 ms | 15 | 15 | 0 % | 532 ms | queuing begins (latency > spacing) |
| 100 ms | 15 | 9 | 40 % | 989 ms | saturated |
| 50 ms | 15 | 6 | 60 % | 820 ms | heavy loss |

Sustainable rate at 64 B is **≈ 2 msg/s** before queue length blows
latency past the spacing window. On-air time for 64 B at SF7 is
≈ 130 ms; the usable duty-cycle ceiling matches theory.

## Bridge capability — through `lora2mqtt`

Bridge bench (`bench_bridge.py`): serial TX, `lora2mqtt` RX → MQTT,
count MQTT arrivals.

### Baseline sweep at 1500 ms spacing

| Size | Sent | Recv via MQTT | Loss | p50 | Max |
|---:|---:|---:|---:|---:|---:|
| 16 B | 10 | 10 | 0 % | 97 ms | 115 ms |
| 64 B | 10 | 10 | 0 % | 174 ms | 194 ms |
| 128 B | 10 | 10 | 0 % | 258 ms | 301 ms |
| 200 B | 10 | 10 | 0 % | 400 ms | 421 ms |
| 220 B | 10 | 10 | 0 % | 428 ms | 447 ms |

End-to-end latency (serial TX → RF → serial RX → `lora2mqtt` →
MQTT) is within ~10 ms of direct-serial RF latency. MQTT
ser/deser + JSON wrap adds negligible overhead.

### Size ceiling through bridge

| Size | Sent | Recv | Loss |
|---:|---:|---:|---:|
| 220 B | 8 | 8 | 0 % |
| **240 B** | 8 | **1** | **87.5 %** |
| **250 B** | 8 | **0** | **100 %** |

Bridge loss at ≥ 240 B mirrors adapter loss. Every frame that
reaches `lora2mqtt`'s serial reader is published to MQTT; frames
the SX1262 fragments are never reconstructed because stream mode
has no framing marker for re-sync.

### Rate ceiling through bridge at 64 B

| Spacing | Sent | Recv | Loss | p50 |
|---:|---:|---:|---:|---:|
| 500 ms | 15 | 15 | 0 % | 150 ms |
| 250 ms | 15 | 14 | 6.7 % | 264 ms |
| 100 ms | 15 | 8 | 46.7 % | 729 ms |

Bridge rate curve tracks the adapter's rate curve. Whatever the
adapter delivers, the bridge forwards.

## Which breaks first

**The adapter, in both dimensions:**

1. **Size** — SX1262 stream mode fragments ≥ 240 B payloads into
   corrupt reassemblies. Whatever the bridge receives, it cleanly
   publishes; at that size the bridge receives corrupted frames or
   none at all.
2. **Rate** — SX1262 on-air time at SF7 dominates the rate ceiling.
   Above ~2 msg/s at 64 B the adapter cannot keep up, and loss is
   100 % adapter-side before the bridge gets a chance.

`lora2mqtt` handled **every single frame** the adapter successfully
demodulated during the entire test run (no dropped publishes, no
OOM, no crash, no stuck threads). Its only contribution is
~10–20 ms of latency per message for JSON ser/deser + MQTT publish.

## Hard-limit summary

| Metric | Value | Source |
|---|---|---|
| Max payload per frame | **220 B** | measured, matches SX1262 firmware fragmentation limit |
| Max sustained rate | **≈ 2 msg/s at 64 B / SF7** | measured |
| Airtime per max-size frame | **≈ 430 ms** | measured (p50, bridge) |
| Airtime per 64 B frame | **≈ 170 ms** | measured (p50, bridge) |
| Goodput at 220 B / 1.5 s spacing | **≈ 1.2 kbps** | measured |
| Goodput at 64 B / 0.5 s spacing | **≈ 1.0 kbps** | measured |
| Theoretical chip rate at SF7 / BW125 / CR4/5 | ≈ 5.5 kbps | Semtech datasheet |
| TX output power | +22 dBm (160 mW) | adapter default |
| RX sensitivity at SF7 | −124 dBm | SX1262 datasheet |
| Link budget (with +2 dBi dipoles) | ≈ 150 dB | TX power + gains − RX sensitivity |
| Theoretical free-space range | ≈ 870 m at sensitivity | FSPL at 868 MHz solving for 150 dB |
| Regulatory sustained rate (EU868 g) | **≈ 1 packet / 35 s** | 1 % duty cycle at 350 ms airtime |
| Bridge ser/deser overhead | **+ 10 – 20 ms** | measured (`bench_bridge` vs `bench`) |
| Bridge sustained capacity | **matches adapter 1:1** | measured, no bridge-side loss |

### Why the size ceiling is exactly 240 B

Semtech allows up to 255 B per LoRa frame. Our adapter caps at
220 B payload (232 B with the 12 B bench framing overhead). At ≥ 240 B
every frame arrives corrupt on the CRC check. Most likely causes:

1. **SX1262 firmware fragmentation.** The Waveshare default firmware
   uses a 240 B internal ring buffer on the UART-to-RF path; payloads
   above that wrap the buffer and emit two fragments the receiver
   cannot recombine in stream mode (no framing marker for re-sync).
2. **Explicit-header frame-length field.** The frame-size field in
   the LoRa PHY header is 8 bits; 255 is the hard limit but PHY +
   link-layer metadata eat into it, leaving ~240 usable.

The adapter gives no error on TX side — bytes are accepted — but
the RX side never sees a good CRC. This is an **adapter hardware
limit**, not a bridge limit.

## Receiver sensitivity ladder (SX1262 datasheet, theoretical)

| SF | BW 125 kHz | BW 500 kHz |
|---:|---:|---:|
| 7 | −124 dBm | −118 dBm |
| 8 | −127 dBm | −121 dBm |
| 9 | −130 dBm | −124 dBm |
| 10 | −133 dBm | −127 dBm |
| 11 | −135 dBm | −129 dBm |
| 12 | −137 dBm | −131 dBm |

Raising SF from 7 to 12 trades ≈ 13 dB of sensitivity (≈ 10× more
range) for 32× more airtime per bit, pushing data rate from
≈ 5.5 kbps at SF7 down to ≈ 250 bps at SF12.

## Bugs found and fixed during the run

In `bench_bridge.py` (the harness, not Hermod):

1. `MqttCollector` called `client.subscribe()` before
   `client.loop_start()`. paho-mqtt v1 silently drops the SUBSCRIBE
   when the network loop isn't running, so no `lora/#` reached the
   handler. **Fix:** moved `subscribe()` into `on_connect` so it
   fires once the network loop is live and survives reconnects.
2. Parser did `raw[idx + len(MAGIC):].split(b"-")` which left a
   leading `-` after the magic prefix — the split produced an empty
   first element and `int()` raised, silently dropping every match.
   **Fix:** skip one byte past the magic before splitting.
3. `bench.py` rounded p50 to one decimal but fell back to unrounded
   `max()` for p95 when `n < 20`. **Fix:** round the fallback too.

No bugs surfaced in `lora2mqtt` itself. The `'H' is an invalid start
of a value` JSON-parse warning in its log is the documented fallback
path in `MqttService.BuildMergedPayload` (line 246): when a received
LoRa frame is not itself valid JSON, the bridge wraps it in
`{"data": …, "rssi": null, "snr": null}` and publishes. Deliberate
behaviour — bench frames are raw ASCII + padding, so the warning
fires once per frame and the wrapper works correctly every time.

## What we cannot claim without more setup

- **Range in situ.** Both adapters were on the same desk throughout;
  no distance, attenuation, or indoor-multipath sweep. Range testing
  requires physically separating the adapters and stepping through
  10 m / 50 m / 100 m / 500 m, indoor and outdoor.
- **Measured RSSI / SNR.** The adapter reports these (it was
  configured with `AT+RSSI=1`) but `WaveshareLoRaAdapter.ParseFrame`
  expects a comma-separated `payload,rssi` form, while the
  stream-mode firmware appends `\n<number>` instead. Every bridged
  message shows `"rssi":null` for this reason — a code-level fix in
  the adapter parser is needed before RSSI / SNR can be cited.
- **Collision behaviour.** One TX + one RX only. Two simultaneous
  TXs on the same channel produce LoRa's well-documented
  capture-effect; we did not exercise it.
- **Higher-SF performance.** SF7 only.
- **Other channels.** Channel 18 only. EU868 has 8 useful channels;
  coexistence with LoRaWAN gateways would need channel-hopping.

## Files

| Path | Purpose |
|---|---|
| `lora-bench/bench.py` | direct-serial packet sweep |
| `lora-bench/bench_bridge.py` | bridge-aware sweep through `lora2mqtt` |
| `lora-bench/probe_loopback.py` | RF loopback validator |
| `raw/<ts>-…F5-lora-bench/` | F5 phase output (loadgen + sampler) |
| `raw/<ts>-…J-bg-lora/` | J-bg-lora phase output (background co-residency) |
