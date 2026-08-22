"""
Three-step probe.

Step 1: DTR pulse on each adapter (some USB-serial reset boards do this).
Step 2: Raw RF loopback — open both ports, write a known pattern on ACM0,
        listen on ACM1 for 2 s, then vice versa.
Step 3: Report raw bytes seen (so we know if the radios are already
        configured for passthrough mode).

No AT handshake required.
"""

from __future__ import annotations

import sys
import time

import serial

PORTS = ["/dev/ttyACM0", "/dev/ttyACM1"]


def pulse_dtr(ser: serial.Serial) -> None:
    for level in (False, True, False):
        ser.dtr = level
        time.sleep(0.1)
    time.sleep(0.3)


def loopback(tx_port: str, rx_port: str, baud: int = 115200) -> None:
    tx = serial.Serial(tx_port, baud, timeout=0.2, write_timeout=1.0)
    rx = serial.Serial(rx_port, baud, timeout=2.0, write_timeout=1.0)
    with tx, rx:
        pulse_dtr(tx)
        pulse_dtr(rx)
        time.sleep(0.5)
        rx.reset_input_buffer()
        tx.reset_output_buffer()
        payload = b"HERMOD_LORA_PROBE_" + bytes(range(16)) + b"_END\r\n"
        tx.write(payload)
        tx.flush()
        got = b""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            chunk = rx.read(128)
            if chunk:
                got += chunk
        print(f"  {tx_port} -> {rx_port} @ {baud}:")
        print(f"    sent  ({len(payload)}B): {payload!r}")
        print(f"    got   ({len(got)}B): {got!r}")


print("=== DTR pulse then RF loopback both ways ===")
loopback("/dev/ttyACM0", "/dev/ttyACM1")
loopback("/dev/ttyACM1", "/dev/ttyACM0")
