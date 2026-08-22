"""
Packet-size + rate sweep for the two USB LoRa adapters on /dev/ttyACM0
and /dev/ttyACM1. They're already configured in stream/pipe mode on the
same channel (verified by loopback probe), so we can just write bytes
on one and read bytes on the other.

The rig sends framed packets (magic prefix + u32 seq + u16 size +
payload + 2-byte CRC16) across sweeping sizes + spacings, counts TX vs
RX, measures latency per packet, and writes a Markdown report.

Usage:
    python3 bench.py \
        --tx /dev/ttyACM1 --rx /dev/ttyACM0 \
        --out-md /tmp/lora-report.md
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
import threading
import time
from pathlib import Path
from typing import List, Dict, Any

import serial


MAGIC = b"HMRD"
HEADER_FMT = ">4sIH"  # magic + seq + payload_size
HEADER_LEN = struct.calcsize(HEADER_FMT)
CRC_LEN = 2


def crc16(buf: bytes) -> int:
    """CRC16-CCITT (XMODEM polynomial 0x1021, init 0x0000)."""
    crc = 0
    for b in buf:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def frame(seq: int, size: int) -> bytes:
    payload = bytes((i & 0xFF for i in range(size)))
    hdr = struct.pack(HEADER_FMT, MAGIC, seq, size)
    crc = crc16(hdr + payload)
    return hdr + payload + struct.pack(">H", crc)


class Receiver:
    """Background thread. Reads a byte stream, re-syncs on the magic
    prefix, parses one frame at a time, records arrival time + seq +
    whether the CRC matched."""

    def __init__(self, port: str, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self._ser: serial.Serial | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.frames: List[Dict[str, Any]] = []
        self.stream_bytes = 0

    def start(self) -> None:
        self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        if self._ser:
            self._ser.close()

    def _loop(self) -> None:
        assert self._ser is not None
        buf = b""
        while not self._stop.is_set():
            chunk = self._ser.read(4096)
            if not chunk:
                continue
            self.stream_bytes += len(chunk)
            buf += chunk
            while True:
                idx = buf.find(MAGIC)
                if idx < 0:
                    # Magic not present; drop all but last 3 bytes (a
                    # magic byte might span the next chunk).
                    buf = buf[-3:]
                    break
                if idx > 0:
                    buf = buf[idx:]
                if len(buf) < HEADER_LEN:
                    break
                _, seq, size = struct.unpack_from(HEADER_FMT, buf, 0)
                frame_len = HEADER_LEN + size + CRC_LEN
                if len(buf) < frame_len:
                    break
                body = buf[:frame_len]
                buf = buf[frame_len:]
                want_crc = struct.unpack(">H", body[-CRC_LEN:])[0]
                got_crc = crc16(body[:-CRC_LEN])
                self.frames.append({
                    "seq": seq,
                    "size": size,
                    "ts": time.monotonic(),
                    "crc_ok": want_crc == got_crc,
                })


def sweep(
    tx_port: str,
    rx_port: str,
    baud: int,
    sizes: List[int],
    count_per_size: int,
    spacing_ms: int,
    settle_ms: int = 500,
) -> List[Dict[str, Any]]:
    rx = Receiver(rx_port, baud)
    rx.start()
    tx = serial.Serial(tx_port, baud, timeout=0.5, write_timeout=2.0)
    results: List[Dict[str, Any]] = []
    global_seq = 0
    try:
        time.sleep(settle_ms / 1000.0)
        for size in sizes:
            send_starts: Dict[int, float] = {}
            seq_range = []
            for _ in range(count_per_size):
                global_seq += 1
                body = frame(global_seq, size)
                send_starts[global_seq] = time.monotonic()
                seq_range.append(global_seq)
                tx.write(body)
                tx.flush()
                time.sleep(spacing_ms / 1000.0)
            # Settle: give slower frames time to arrive.
            settle_after = max(2.0, size * 0.01)
            time.sleep(settle_after)
            sent = len(seq_range)
            recv = [f for f in rx.frames if f["seq"] in send_starts]
            rx_ok = [f for f in recv if f["crc_ok"]]
            latencies_ms = [
                (f["ts"] - send_starts[f["seq"]]) * 1000.0
                for f in rx_ok
            ]
            results.append({
                "size_bytes": size,
                "frame_bytes": HEADER_LEN + size + CRC_LEN,
                "spacing_ms": spacing_ms,
                "sent": sent,
                "received": len(rx_ok),
                "received_corrupt": len(recv) - len(rx_ok),
                "loss_pct": round((sent - len(rx_ok)) / sent * 100.0, 2),
                "latency_ms_p50": round(statistics.median(latencies_ms), 1) if latencies_ms else None,
                "latency_ms_p95": (
                    round(statistics.quantiles(latencies_ms, n=20)[18], 1)
                    if len(latencies_ms) >= 20
                    else round(max(latencies_ms), 1) if latencies_ms else None
                ),
                "latency_ms_max": round(max(latencies_ms), 1) if latencies_ms else None,
            })
    finally:
        tx.close()
        rx.stop()
    return results


def render_markdown(rows: List[Dict[str, Any]], tx_port: str, rx_port: str,
                    baud: int, notes: List[str]) -> str:
    lines: List[str] = []
    lines.append("# LoRa adapter packet-size sweep")
    lines.append("")
    lines.append(f"- TX adapter: `{tx_port}`")
    lines.append(f"- RX adapter: `{rx_port}`")
    lines.append(f"- Baud: {baud}")
    lines.append(f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}")
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append("| size | frame | spacing | sent | recv | corrupt | loss% | p50 ms | p95 ms | max ms |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        lines.append(
            f"| {r['size_bytes']} | {r['frame_bytes']} | {r['spacing_ms']} |"
            f" {r['sent']} | {r['received']} | {r['received_corrupt']} |"
            f" {r['loss_pct']} |"
            f" {r['latency_ms_p50'] if r['latency_ms_p50'] is not None else '—'} |"
            f" {r['latency_ms_p95'] if r['latency_ms_p95'] is not None else '—'} |"
            f" {r['latency_ms_max'] if r['latency_ms_max'] is not None else '—'} |"
        )
    if notes:
        lines.append("")
        lines.append("## Notes")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--tx", default="/dev/ttyACM1")
    ap.add_argument("--rx", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--sizes", default="16,32,64,128,200,240")
    ap.add_argument("--count", type=int, default=20,
                    help="packets per size")
    ap.add_argument("--spacing-ms", type=int, default=1000)
    ap.add_argument("--out-md", type=Path, default=Path("/tmp/lora-report.md"))
    ap.add_argument("--out-json", type=Path, default=Path("/tmp/lora-report.json"))
    args = ap.parse_args()

    sizes = [int(s) for s in args.sizes.split(",")]
    rows = sweep(args.tx, args.rx, args.baud, sizes, args.count, args.spacing_ms)
    args.out_json.write_text(json.dumps({
        "tx": args.tx, "rx": args.rx, "baud": args.baud,
        "spacing_ms": args.spacing_ms, "count_per_size": args.count,
        "results": rows,
    }, indent=2))
    args.out_md.write_text(render_markdown(rows, args.tx, args.rx, args.baud, []))
    print(f"wrote {args.out_md}")
    print(f"wrote {args.out_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
