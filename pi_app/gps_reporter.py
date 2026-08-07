"""GPS reporting for the checkpoint device.

No GNSS module is wired up yet — this exists so the day one is, plugging
it in and setting two environment variables is the whole job. Until then
`open_gps()` falls back to a reader that never produces a fix, and the
location stays whatever an admin set by hand in the console.

Two implementations:

  SerialGPSReader   any NMEA-0183 module on a serial/UART port (e.g. the
                    common NEO-6M), via `pyserial` + `pynmea2`
  NullGPSReader     no module attached — reports nothing

`open_gps()` picks the real one when its libraries are importable and the
configured port is open, and falls back otherwise — same shape as
badge_reader.open_reader(), so a Pi with no module attached still starts.
"""

from __future__ import annotations

import os
import threading
import time


class GPSReader:
    """Holds the most recent (lat, lng) fix. Subclasses run their own thread."""

    name = "gps"

    def __init__(self):
        self._lock = threading.Lock()
        self._fix: tuple[float, float] | None = None
        self._running = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def latest(self) -> tuple[float, float] | None:
        """The most recent fix received, or None if there isn't one yet."""
        with self._lock:
            return self._fix

    def _set_fix(self, lat: float, lng: float) -> None:
        with self._lock:
            self._fix = (lat, lng)

    def _loop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class SerialGPSReader(GPSReader):
    """Any NMEA-0183 module (e.g. NEO-6M) on a serial/UART port.

    Requires:
        pip install pyserial pynmea2

    Wiring is whatever the module needs — most are UART (TX/RX/GND/VCC),
    not SPI/I2C like the badge reader, so SAFETYFIRST_GPS_PORT is the
    thing to change per board, not this code.
    """

    name = "serial NMEA"

    def __init__(self, port: str, baud: int = 9600):
        super().__init__()
        import serial  # imported late: hardware-only dependency

        self._serial = serial.Serial(port, baud, timeout=1)

    def _loop(self) -> None:
        import pynmea2

        while self._running:
            try:
                raw = self._serial.readline()
            except Exception:  # noqa: BLE001 - a bad read shouldn't kill the gate
                time.sleep(0.5)
                continue

            line = raw.decode("ascii", errors="ignore").strip()
            if not line.startswith(("$GPGGA", "$GPRMC", "$GNGGA", "$GNRMC")):
                continue

            try:
                msg = pynmea2.parse(line)
            except pynmea2.ParseError:
                continue

            lat, lng = getattr(msg, "latitude", None), getattr(msg, "longitude", None)
            if not lat and not lng:
                continue  # a sentence before the module has a fix reads as all-zero
            self._set_fix(lat, lng)

    def stop(self) -> None:
        super().stop()
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001
            pass


class NullGPSReader(GPSReader):
    """No module attached — never produces a fix."""

    name = "none (no GPS module detected)"

    def _loop(self) -> None:
        while self._running:
            time.sleep(1.0)


def open_gps() -> GPSReader:
    """Return the best available GPS source.

    SAFETYFIRST_GPS=off (the default, absent any setting) always returns
    the null reader; SAFETYFIRST_GPS=serial forces the serial reader and
    raises if it can't open; "auto" tries the serial port and falls back
    quietly — a Pi with no module attached shouldn't refuse to start the
    gate over it.
    """
    preference = os.environ.get("SAFETYFIRST_GPS", "off").lower()
    port = os.environ.get("SAFETYFIRST_GPS_PORT", "/dev/ttyUSB0")

    if preference == "off":
        return NullGPSReader()

    if preference in ("auto", "serial"):
        try:
            return SerialGPSReader(port)
        except Exception as exc:  # noqa: BLE001 - missing libs, no port, etc.
            if preference == "serial":
                raise SystemExit(f"GPS serial port unavailable: {exc}")
            print(f"[gps] No GPS module detected ({exc}); location stays whatever the console has set")

    return NullGPSReader()
