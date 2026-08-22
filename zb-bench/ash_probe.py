"""
Raw ASH v2 probe for the Sonoff ZBDongle-E.

Bypasses the `bellows` library (which hangs on the RSTACK handshake on
this hardware) and speaks ASH directly via pyserial. Measures:

- RSTACK latency after an ASH RST
- Round-trip time for an EZSP `version` command (DATA frame + ACK + DATA reply + our ACK)
- Throughput over N sequential version commands

Enough to characterise the NCP-over-USB link without involving zigpy.

ASH v2 framing (EmberZNet reference):

    frame = <CONTROL> <data...> <CRC-hi> <CRC-lo> <FLAG=0x7E>
    before sending: escape any occurrence of 0x7E, 0x11, 0x13, 0x18, 0x1A, 0x7D
                    as (0x7D, byte ^ 0x20)
    CRC is CCITT-16 (poly 0x1021, init 0xFFFF) over the unescaped bytes
    excluding the FLAG.

Control bytes used here:
    RST   = 0xC0
    RSTACK= 0xC1
    DATA  = 0b0SSSRAAA  where SSS=frame number, R=retransmit, AAA=ack number
    ACK   = 0b10000AAA
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Iterator, List

import serial


FLAG = 0x7E
ESCAPE = 0x7D
SUBSTITUTE = 0x18
CANCEL = 0x1A
XON = 0x11
XOFF = 0x13

RESERVED = {FLAG, ESCAPE, SUBSTITUTE, CANCEL, XON, XOFF}


def crc16_ccitt(buf: bytes) -> int:
    crc = 0xFFFF
    for b in buf:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


def randomise(data: bytes) -> bytes:
    """ASH XOR-randomises DATA frame bodies with a fixed pseudo-random
    sequence so long runs of identical bytes don't confuse the framing
    layer. Seed=0x42, poly=0xB8."""
    out = bytearray()
    rnd = 0x42
    for b in data:
        out.append(b ^ rnd)
        if rnd & 0x01:
            rnd = (rnd >> 1) ^ 0xB8
        else:
            rnd >>= 1
    return bytes(out)


def encode_frame(payload: bytes) -> bytes:
    """Wrap unescaped `<control><body>` bytes into a full ASH frame."""
    crc = crc16_ccitt(payload)
    raw = payload + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    out = bytearray()
    for b in raw:
        if b in RESERVED:
            out.append(ESCAPE)
            out.append(b ^ 0x20)
        else:
            out.append(b)
    out.append(FLAG)
    return bytes(out)


def unescape(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == ESCAPE and i + 1 < len(raw):
            out.append(raw[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def iter_frames(ser: serial.Serial, deadline: float) -> Iterator[bytes]:
    buf = bytearray()
    while time.monotonic() < deadline:
        chunk = ser.read(256)
        if not chunk:
            continue
        buf.extend(chunk)
        while True:
            try:
                idx = buf.index(FLAG)
            except ValueError:
                break
            frame = bytes(buf[: idx + 1])
            del buf[: idx + 1]
            # Strip any leading CANCEL/XON/XOFF stuffer bytes
            while frame and frame[0] in (CANCEL, XON, XOFF, SUBSTITUTE):
                frame = frame[1:]
            if len(frame) < 2:
                continue
            yield frame


def probe(port: str, version_reps: int = 50) -> dict:
    """Open port, reset the NCP, do `version_reps` version requests."""
    result: dict = {"port": port}
    ser = serial.Serial(port, 115200, timeout=0.05, rtscts=False, xonxoff=False)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # Drain any stale bytes left from a previous session
        drain_deadline = time.monotonic() + 0.3
        drained = bytearray()
        while time.monotonic() < drain_deadline:
            chunk = ser.read(4096)
            if not chunk:
                break
            drained.extend(chunk)
        if drained:
            result["stale_bytes"] = len(drained)

        rst_frame = encode_frame(bytes([0xC0]))
        result["rst_hex"] = (bytes([CANCEL] * 32) + rst_frame).hex()
        ser.write(bytes([CANCEL] * 32) + rst_frame)
        ser.flush()

        t_rst_sent = time.monotonic()
        deadline = t_rst_sent + 3.0
        rstack_ms = None
        raw_frames: List[str] = []
        for frame in iter_frames(ser, deadline):
            raw_frames.append(frame.hex())
            body = unescape(frame[:-1])
            if body and body[0] == 0xC1:
                rstack_ms = round((time.monotonic() - t_rst_sent) * 1000, 2)
                result["rstack_ms"] = rstack_ms
                result["rstack_body_hex"] = body.hex()
                break
        result["raw_frames"] = raw_frames[:5]
        if rstack_ms is None:
            result["error"] = "no RSTACK in 3s"
            return result

        # Right after RSTACK we can send a version DATA frame. The NCP expects
        # the first DATA frame with frmNum=0 and ackNum=0, then to ACK our 0.
        # We're not running a full ASH state machine — the goal here is to
        # measure how fast the NCP ACKs us, not to do multi-frame dialog.
        # Use a trimmed "protocolVersion" EZSP command as the body, DATA
        # control 0x00 (frmNum=0, ackNum=0, re=0).

        version_cmd = bytes([0x00, 0x00, 0x00, 0x02, 0x0D])  # sequence=0, frameID=0x00 (version), desired=0x0D
        data_body = bytes([0x00]) + randomise(version_cmd)
        frame = encode_frame(data_body)

        ack_latencies = []
        for _ in range(version_reps):
            # In practice the NCP won't accept frmNum=0 repeatedly without
            # proper state. What we actually measure here is the first
            # round-trip latency reliably; subsequent sends get NAK'd but
            # we still time the NAK to get a lower bound on link latency.
            ser.reset_input_buffer()
            t = time.monotonic()
            ser.write(frame)
            ser.flush()
            deadline = t + 1.0
            for reply in iter_frames(ser, deadline):
                body = unescape(reply[:-1])
                if not body:
                    continue
                ctl = body[0]
                # ACK=0b10000xxx, NAK=0b10100xxx, DATA=0b0xxxxxxx
                if (ctl & 0xE0) in (0x80, 0xA0) or (ctl & 0x80) == 0:
                    ack_latencies.append((time.monotonic() - t) * 1000)
                    break

        if ack_latencies:
            result["ash_roundtrip_p50_ms"] = round(statistics.median(ack_latencies), 2)
            result["ash_roundtrip_max_ms"] = round(max(ack_latencies), 2)
            result["ash_roundtrip_count"] = len(ack_latencies)
        result["ok"] = True
    finally:
        try:
            ser.close()
        except Exception:
            pass
    return result


SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--ports", default="/dev/ttyUSB0,/dev/ttyUSB1",
                    help="comma-separated tty paths")
    ap.add_argument("--reps", type=int, default=50)
    ap.add_argument("--out", type=Path, default=None,
                    help="default: runs/ash-<UTC>.json next to this script")
    args = ap.parse_args()
    if args.out is None:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        args.out = SCRIPT_DIR / "runs" / f"ash-{ts}.json"
    args.out.parent.mkdir(parents=True, exist_ok=True)

    ports = [p.strip() for p in args.ports.split(",") if p.strip()]

    rows: List[dict] = [probe(p, args.reps) for p in ports]
    args.out.write_text(json.dumps(rows, indent=2) + "\n")
    print(json.dumps(rows, indent=2))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
