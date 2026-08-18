"""GPS reporting for the checkpoint device.

Three implementations:

  QuectelGPSReader  the Vanix TracX-1b (Quectel EC200U-CN), which is the
                    GNSS receiver this project actually owns
  SerialGPSReader   any NMEA-0183 module that streams unprompted on a
                    serial/UART port (e.g. a NEO-6M), via `pyserial` +
                    `pynmea2`
  NullGPSReader     no module attached — reports nothing

`open_gps()` picks the best available and falls back otherwise — same
shape as badge_reader.open_reader(), so a Pi with no module attached
still starts.

The EC200U is not a NEO-6M and SerialGPSReader cannot drive it. It stays
silent until `AT+QGPS=1` switches the receiver on, its UART runs at
115200 rather than 9600, and it presents several USB interfaces of which
only one answers AT commands. So it gets its own reader that asks for a
fix instead of waiting to be told one.

Ports are found by USB vendor id, never by walking /dev/ttyUSB* in
order. The gate master ESP32 is a CP210x sitting on that same list, and
writing AT commands at it would corrupt the badge stream that
serial_bridge.py is reading — the one wire the gate cannot afford to
lose. Filtering on the vendor id means we only ever open the modem.

The board enumerates seven interfaces and only some answer AT; the
others return binary diagnostics, nothing at all, or are already held by
ModemManager. Which number lands where moves with enumeration order, so
the usable one is found by asking rather than by being hardcoded.
"""

from __future__ import annotations

import os
import re
import threading
import time

# Quectel Wireless Solutions. Matched on the vendor id and not the
# product string, because this board reports itself as "Android" —
# /dev/serial/by-id lists it as usb-Android_Android-ifNN-port0, so a
# name match finds nothing. The vendor id is the part that wasn't
# overwritten (confirmed on the device: ATI reports EC200U).
QUECTEL_VID = 0x2C7C


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


def _quectel_ports() -> list[str]:
    """Serial ports belonging to a Quectel modem, by USB vendor id."""
    try:
        from serial.tools import list_ports  # part of pyserial
    except ImportError:
        return []

    return [port.device
            for port in sorted(list_ports.comports(), key=lambda p: p.device)
            if port.vid == QUECTEL_VID]


class QuectelGPSReader(GPSReader):
    """The Vanix TracX-1b (Quectel EC200U-CN), over its AT command port.

    Requires:
        pip install pyserial

    No pynmea2 here: the module answers a location request with a single
    +QGPSLOC line, so there is no NMEA stream to parse.
    """

    name = "Quectel EC200U (TracX-1b)"

    # +QGPSLOC: <UTC>,<lat>,<lon>,<HDOP>,<alt>,<fix>,<cog>,<spkm>,<spkn>,<date>,<nsat>
    # Mode 2 reports degrees as a signed decimal, which is what we store.
    _LOC = re.compile(r"\+QGPSLOC:\s*[^,]*,(-?\d+\.\d+),(-?\d+\.\d+)")

    def __init__(self, port: str, baud: int = 115200, poll: float = 2.0):
        super().__init__()
        import serial  # imported late: hardware-only dependency

        self._poll = poll
        self._serial = serial.Serial(port, baud, timeout=1)

        # Several of the modem's interfaces open happily and then never
        # answer. Only the one that replies to a bare AT is usable, so
        # prove it here rather than discovering it in the poll loop.
        if not any(line == "OK" for line in self._command("AT", timeout=2.0)):
            self._serial.close()
            raise OSError(f"{port} opened but did not answer AT")

        self.name = f"Quectel EC200U (TracX-1b) on {os.path.basename(port)}"
        self._enable_gnss()

    def _command(self, text: str, timeout: float = 3.0) -> list[str]:
        try:
            self._serial.reset_input_buffer()
            self._serial.write((text + "\r\n").encode("ascii"))
        except Exception:  # noqa: BLE001 - a bad write shouldn't kill the gate
            return []

        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = self._serial.readline()
            except Exception:  # noqa: BLE001
                break
            line = raw.decode("ascii", errors="ignore").strip()
            if not line or line == text:
                continue  # the modem echoes the command back first
            lines.append(line)
            if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
                break
        return lines

    def _enable_gnss(self) -> None:
        """Switch the receiver on, tolerating it already being on.

        `+CME ERROR: 504` means the GNSS session is already active — the
        normal reply when the gate process restarts and the modem was
        never power-cycled. Treating that as failure would mean GPS only
        ever worked on the first run after a cold boot.
        """
        for line in self._command("AT+QGPS=1", timeout=5.0):
            if line == "OK" or "504" in line:
                return
        # Worth seeing, but not worth refusing to start over: the poll
        # loop simply reports no fix and the console keeps whatever an
        # admin set by hand.
        print("[gps] AT+QGPS=1 was not acknowledged; continuing without a fix")

    def _loop(self) -> None:
        while self._running:
            # `+CME ERROR: 516` (not fixed yet) is the expected answer
            # until the antenna has sky view, so no match simply means
            # keep the previous fix and try again.
            for line in self._command("AT+QGPSLOC=2", timeout=3.0):
                match = self._LOC.search(line)
                if match:
                    self._set_fix(float(match.group(1)), float(match.group(2)))
                    break
            time.sleep(self._poll)

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

        off      (the default) always returns the null reader
        auto     find a Quectel by name; failing that use
                 SAFETYFIRST_GPS_PORT, but only if it was set explicitly
        quectel  force the TracX-1b, and fail loudly if it isn't there
        serial   force a plain NMEA module on SAFETYFIRST_GPS_PORT

    "auto" deliberately does not fall back to the old /dev/ttyUSB0
    default. On this Pi that port is the gate master ESP32, and opening
    it would put AT chatter on the wire serial_bridge.py reads badges
    from. A gate that stops recognising cards is a far worse outcome
    than a gate that doesn't know where it is, so an unconfigured port
    is left alone rather than guessed at.
    """
    preference = os.environ.get("SAFETYFIRST_GPS", "off").lower()
    configured = os.environ.get("SAFETYFIRST_GPS_PORT")

    if preference == "off":
        return NullGPSReader()

    if preference in ("auto", "quectel"):
        for path in _quectel_ports():
            try:
                return QuectelGPSReader(path)
            except Exception:  # noqa: BLE001 - wrong interface, or no pyserial
                continue
        if preference == "quectel":
            raise SystemExit(
                "No Quectel modem found. Check it is plugged in and powered "
                "(ls /dev/serial/by-id/)."
            )

    if preference == "serial" or (preference == "auto" and configured):
        try:
            return SerialGPSReader(configured or "/dev/ttyUSB0")
        except Exception as exc:  # noqa: BLE001 - missing libs, no port, etc.
            if preference == "serial":
                raise SystemExit(f"GPS serial port unavailable: {exc}")
            print(f"[gps] No GPS module detected ({exc}); location stays whatever the console has set")

    if preference == "auto":
        print("[gps] No GNSS module detected; location stays whatever the console has set")
    return NullGPSReader()
