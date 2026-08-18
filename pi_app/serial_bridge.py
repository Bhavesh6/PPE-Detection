"""The master ESP32, over USB.

Sensor nodes talk ESP-NOW to a master board; the master is wired to the Pi by
USB and forwards everything down one serial line. That covers two kinds of
traffic which used to arrive separately — badge scans (previously an RC522 on
the Pi's own SPI header) and hazard reports (previously an HTTP POST) — so
this is a `BadgeReader` that also feeds the local alert store, rather than
two objects fighting over one port. Only one process can hold a serial
device open, so they could not be split even if it were tidier.

Line protocol, one message per line, verb first. Case-insensitive, and any
line that doesn't parse is ignored rather than logged — an ESP32 prints a
burst of bootloader noise at every reset, and treating that as an error
would fill the log each time the board is power-cycled:

    BADGE 0006238412
    ALERT gas critical fumes near the mixer
    READING gas 450 ppm

`READING` carries a raw value and is classified against the site thresholds
the roster sync caches, exactly as the HTTP receiver does — the same ppm has
to mean the same thing whichever way it reached the Pi.

The port is reopened if it disappears, so unplugging the master and plugging
it back in recovers on its own, matching how the camera behaves.
"""

from __future__ import annotations

import glob
import os
import time

from badge_reader import BadgeReader

BAUD = int(os.environ.get("SAFETYFIRST_SERIAL_BAUD", "115200"))
RECONNECT_SECONDS = 3.0
# Same reasoning as the RC522 path: dedupe on time, not identity, so a worker
# turned away can fix their gear and re-present the same badge.
REPEAT_LOCKOUT_SECONDS = 3.0


def find_port() -> str | None:
    """First likely master board, or None.

    USB-UART bridges (CH340, CP2102) appear as ttyUSB*; boards with native
    USB as ttyACM*. Both are plausible depending on which ESP32 is used.
    """
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return None


class SerialBridgeReader(BadgeReader):
    """Badge scans and hazard reports arriving from the master over USB."""

    name = "ESP32 master (USB serial)"

    def __init__(self, port: str | None = None, alerts=None, policy_provider=None):
        super().__init__()
        import serial  # late: only needed when a board is actually attached

        self._serial = serial
        self._port = port or os.environ.get("SAFETYFIRST_SERIAL_PORT") or ""
        self._alerts = alerts
        self._policy = policy_provider or (lambda: {})
        self._conn = None

    # -- connection ------------------------------------------------------
    def _open(self) -> bool:
        port = self._port or find_port()
        if not port:
            return False
        try:
            # A read timeout rather than blocking, so stop() can end this
            # thread instead of it sitting in readline() forever.
            self._conn = self._serial.Serial(port, BAUD, timeout=1)
            self.name = f"ESP32 master ({port})"
            return True
        except (OSError, self._serial.SerialException):
            self._conn = None
            return False

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 - already going away
                pass
            self._conn = None

    # -- protocol --------------------------------------------------------
    def _handle(self, line: str, last: dict) -> None:
        parts = line.split()
        if len(parts) < 2:
            return
        verb = parts[0].upper()

        if verb == "BADGE":
            tag = parts[1].strip()
            now = time.monotonic()
            if not tag:
                return
            if tag != last.get("tag") or now - last.get("at", 0.0) >= REPEAT_LOCKOUT_SECONDS:
                last["tag"] = tag
                last["at"] = now
                self.tags.put(tag)
            return

        if self._alerts is None:
            return

        if verb == "ALERT" and len(parts) >= 3:
            kind, severity = parts[1], parts[2].lower()
            if severity not in ("critical", "warning", "info"):
                return
            message = " ".join(parts[3:])
            self._alerts.record(kind, severity, message, source="esp32-master")
            print(f"[serial] {severity} {kind} alert from the master")
            return

        if verb == "READING" and len(parts) >= 3:
            from local_alerts import evaluate

            kind = parts[1]
            try:
                value = float(parts[2])
            except ValueError:
                return
            unit = parts[3] if len(parts) > 3 else ""
            thresholds = (self._policy() or {}).get("sensor_thresholds") or {}
            severity, _cfg = evaluate(kind, value, thresholds)
            if severity is None:
                return          # below threshold, or none configured
            self._alerts.record(kind, severity, f"{kind} reading {value}{unit}",
                                source="esp32-master", value=value)
            print(f"[serial] {kind} {value}{unit} crossed {severity}")

    # -- loop ------------------------------------------------------------
    def _loop(self) -> None:
        last: dict = {}
        while self._running:
            if self._conn is None:
                if not self._open():
                    time.sleep(RECONNECT_SECONDS)
                    continue

            try:
                raw = self._conn.readline()
            except Exception:  # noqa: BLE001
                # Deliberately broad. Two different things land here: the
                # master being unplugged mid-read, and stop() closing the
                # port underneath this thread at shutdown. Neither may kill
                # the loop — a dead reader thread means badges silently stop
                # being read, with nothing on screen to say why.
                if not self._running:
                    break
                self._close()
                print("[serial] master disconnected — waiting for it")
                time.sleep(RECONNECT_SECONDS)
                continue

            if not raw:
                continue        # idle timeout, not an error

            try:
                line = raw.decode("utf-8", errors="replace").strip()
            except Exception:  # noqa: BLE001
                continue
            if line:
                self._handle(line, last)

    def stop(self) -> None:
        super().stop()
        self._close()
