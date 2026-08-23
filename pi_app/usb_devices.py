"""Which USB serial device is which.

Everything the checkpoint owns now arrives on USB: the gate master ESP32
and the Quectel GNSS modem. Both enumerate as generic serial ports, so
"the first /dev/ttyUSB*" is not an answer - the modem presents seven
interfaces and usually wins that race, at which point the gate opens the
modem, reads no badges, and looks broken for a reason nothing logs.

So ports are identified rather than counted:

  1. by USB vendor id, which says what bridge chip is on the board;
  2. by asking, for anything that could be the master, because a CP210x
     is a CP210x whether it sits on our gate or on a bench multimeter;
  3. by USB serial number once found, so replugging into a different
     socket finds the same physical board rather than the same path.

Claims exist because only one process may hold a serial device open, and
the two readers here run in one process. A port claimed by the GPS is
invisible to the gate and the other way round, which is what stops the
AT-command wire and the badge wire from ever being the same wire.

Cross-platform on purpose: this walks pyserial's port list rather than
globbing /dev, so the same code finds COM7 on a Windows bench as finds
/dev/ttyUSB0 on the Pi.
"""

from __future__ import annotations

import os
import re
import threading
import time

# USB-UART bridges an ESP32 dev board is plausibly behind, plus the
# ESP32-S2/S3/C3 native USB device. None of these prove it is our board,
# which is what the probe below is for - they only narrow the field.
ESP_VIDS = {
    0x10C4,  # Silicon Labs CP210x - the classic DevKitC
    0x1A86,  # WCH CH340/CH9102 - most clones
    0x0403,  # FTDI FT232
    0x303A,  # Espressif native USB-JTAG/serial
}

# Vendor ids that are definitely something else. Probing these would put
# stray bytes on a wire that matters, so they are never opened looking
# for a gate master.
GPS_VIDS = {0x2C7C}          # Quectel Wireless Solutions (EC200U/TracX-1b)
NEVER_PROBE = GPS_VIDS | {
    0x0525,  # Linux gadget / RNDIS
    0x1D6B,  # Linux Foundation root hubs
}

# What the master answers with. Firmware prints this at boot and on ID?.
_IDENTITY = re.compile(r"#\s*SAFETYFIRST\s+(\S+)", re.I)
# Failing that, any line in the gate's own protocol is proof enough: no
# other device on this bench emits these verbs.
_PROTOCOL = re.compile(r"^(BADGE|ALERT|READING|FAN)\b", re.I)
# The pre-identity firmware only ever said this. Kept so a board flashed
# before the ID? command still gets recognised, instead of silently
# failing to be found by a Pi that has been updated.
_LEGACY = re.compile(r"#\s*gate master ready", re.I)

PROBE_SECONDS = float(os.environ.get("SAFETYFIRST_USB_PROBE_SECONDS", "3.5"))
BAUD = int(os.environ.get("SAFETYFIRST_SERIAL_BAUD", "115200"))

_lock = threading.Lock()
_claims: dict[str, str] = {}       # device path -> owner name
_last_master: dict = {}            # serial/device/role/at of the last confirmed master
# How long a confirmed master stays trusted without re-probing. The
# reconnect loop runs every few seconds and a probe costs seconds, so
# without this a board that is present would be interrogated forever.
CACHE_SECONDS = float(os.environ.get("SAFETYFIRST_USB_CACHE_SECONDS", "60"))


# -- inventory -----------------------------------------------------------
def _comports():
    try:
        from serial.tools import list_ports  # part of pyserial
    except ImportError:
        return []
    return sorted(list_ports.comports(), key=lambda p: p.device)


def describe_all() -> list[dict]:
    """Every serial port with what we make of it. For doctor.py."""
    out = []
    for p in _comports():
        vid = p.vid
        if vid in GPS_VIDS:
            role = "gps"
        elif vid in ESP_VIDS:
            role = "esp32?"
        else:
            role = "unknown"
        out.append({
            "device": p.device,
            "vid": vid,
            "pid": p.pid,
            "serial": p.serial_number,
            "description": p.description,
            "role": role,
            "claimed_by": claimed_by(p.device),
        })
    return out


# -- claims --------------------------------------------------------------
def claim(device: str, owner: str) -> bool:
    """Take a port for `owner`. False if someone else already holds it."""
    with _lock:
        held = _claims.get(device)
        if held is not None and held != owner:
            return False
        _claims[device] = owner
        return True


def release(device: str | None, owner: str) -> None:
    if not device:
        return
    with _lock:
        if _claims.get(device) == owner:
            del _claims[device]


def claimed_by(device: str) -> str | None:
    with _lock:
        return _claims.get(device)


def _free(device: str, owner: str) -> bool:
    with _lock:
        held = _claims.get(device)
        return held is None or held == owner


# -- the GNSS modem ------------------------------------------------------
def gps_ports(owner: str = "gps") -> list[str]:
    """Quectel interfaces, by vendor id, minus anything already claimed."""
    return [p.device for p in _comports()
            if p.vid in GPS_VIDS and _free(p.device, owner)]


# -- the gate master -----------------------------------------------------
def _probe(device: str) -> str | None:
    """Open `device` and decide whether it is the gate master.

    Returns the role string it identified as, or None.

    Opening the port resets an ESP32 (the bridge toggles DTR/RTS), so the
    boot banner arrives on its own and is the most reliable signal there
    is. ID? is still sent, for a board that was already up and for the
    S3's native USB, which does not reset this way.
    """
    try:
        import serial
    except ImportError:
        return None

    try:
        conn = serial.Serial()
        conn.port = device
        conn.baudrate = BAUD
        conn.timeout = 0.3
        # Do NOT let the driver yank DTR/RTS on open. On a CP2102 that is
        # the ESP32's reset line, so merely identifying the board rebooted
        # it - dropping ESP-NOW peers and fan control - and the probe then
        # spent its whole window reading a board that was still booting.
        # Measured on the gate: the first call returned None and only a
        # second succeeded, which is how the badge reader ended up on the
        # keyboard fallback for an entire session. Left alone, a running
        # master is already talking and matches on its first line.
        conn.dtr = False
        conn.rts = False
        conn.open()
    except Exception:  # noqa: BLE001 - busy, gone, or no permission
        return None

    try:
        conn.reset_input_buffer()
        deadline = time.monotonic() + PROBE_SECONDS
        next_prompt = 0.0
        while time.monotonic() < deadline:
            # Re-ask periodically rather than once. A board that was mid-
            # reset when we opened misses a single ID?, and an idle master
            # with nothing to report would otherwise stay silent until the
            # window expired.
            now = time.monotonic()
            if now >= next_prompt:
                next_prompt = now + 1.2
                try:
                    conn.write(b"ID?\n")
                    conn.flush()
                except Exception:  # noqa: BLE001 - read-only or one-way device
                    pass
            try:
                raw = conn.readline()
            except Exception:  # noqa: BLE001 - yanked mid-probe
                return None
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            found = _IDENTITY.search(line)
            if found:
                return found.group(1)
            if _PROTOCOL.match(line) or _LEGACY.search(line):
                return "gate-master"
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def find_gate_master(owner: str = "gate", probe: bool = True) -> str | None:
    """The port the gate master is actually on, or None.

    Candidates are ESP-like vendor ids that nobody else has claimed. The
    board confirmed last time is tried first, matched on USB serial
    number so it survives being moved to another socket.
    """
    # A port with no vendor id is not on USB at all - the Pi's own GPIO
    # UART, a Bluetooth rfcomm, a motherboard COM1. The master is a USB
    # device by definition here, so those are skipped rather than probed:
    # a bench machine can list half a dozen of them and each costs a
    # multi-second timeout to rule out.
    usb = [p for p in _comports()
           if p.vid is not None
           and p.vid not in NEVER_PROBE
           and _free(p.device, owner)]
    # Known bridge chips first; anything else USB gets a second pass, so
    # an unfamiliar clone is still found without hardcoding its vid.
    known = [p for p in usb if p.vid in ESP_VIDS]
    rest = [p for p in usb if p.vid not in ESP_VIDS]

    remembered = _last_master.get("serial")
    if remembered:
        known.sort(key=lambda p: p.serial_number != remembered)
        rest.sort(key=lambda p: p.serial_number != remembered)

    candidates = known + rest
    if not candidates:
        return None

    if not probe:
        return candidates[0].device

    # Already confirmed recently and still plugged in: trust it. Anything
    # else means re-interrogating a working board on every reconnect.
    cached = _last_master.get("device")
    if cached and time.monotonic() - _last_master.get("at", 0.0) < CACHE_SECONDS:
        if any(p.device == cached for p in candidates):
            return cached

    # Two passes. The badge wire is the one thing a gate cannot do without,
    # and the cost of giving up too early is not a retry a second later -
    # open_reader() falls through to the keyboard and stays there for the
    # life of the process. A board that was resetting, busy, or mid-flash
    # on the first pass deserves a second look before that happens.
    for attempt in range(2):
        for port in candidates:
            role = _probe(port.device)
            if role is None:
                continue
            _last_master["serial"] = port.serial_number or ""
            _last_master["device"] = port.device
            _last_master["role"] = role
            _last_master["at"] = time.monotonic()
            return port.device
        if attempt == 0 and candidates:
            time.sleep(0.5)
    return None


def last_master_role() -> str | None:
    return _last_master.get("role")


def forget_master() -> None:
    """Drop the cached identity, forcing a fresh probe next time.

    Called when a confirmed port stops working: the board may have been
    unplugged and something else may now be on that path.
    """
    _last_master.clear()
