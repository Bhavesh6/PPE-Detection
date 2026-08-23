"""Badge readers for the checkpoint.

The gate app doesn't care how a badge arrives — it just wants a tag string.
Keeping that behind one interface means swapping an MFRC522 for a PN532 or a
Wiegand reader later touches this file only.

Two implementations ship:

  MFRC522Reader   the RC522 module over SPI on a Raspberry Pi
  KeyboardReader  type a tag and press Enter — for developing without hardware

`open_reader()` picks the real one when its libraries are importable and the
SPI device exists, and falls back to the keyboard otherwise.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time


class BadgeReader:
    """Emits badge tags on a queue. Subclasses run their own thread."""

    name = "reader"

    def __init__(self):
        self.tags: queue.Queue[str] = queue.Queue()
        self._running = True
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def read(self) -> str | None:
        """Non-blocking: returns the next scanned tag, or None."""
        try:
            return self.tags.get_nowait()
        except queue.Empty:
            return None

    def _loop(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class MFRC522Reader(BadgeReader):
    """RC522 over SPI.

    Requires SPI enabled (`sudo raspi-config` → Interface Options → SPI) and:

        pip install mfrc522 RPi.GPIO spidev

    Wiring (BCM):
        SDA→GPIO8(CE0)  SCK→GPIO11  MOSI→GPIO10  MISO→GPIO9
        RST→GPIO25      3.3V→3.3V   GND→GND      IRQ→unused
    """

    name = "MFRC522 (SPI)"

    def __init__(self):
        super().__init__()
        from mfrc522 import SimpleMFRC522  # imported late: Pi-only dependency

        self._reader = SimpleMFRC522()

    # How long the same card is ignored after a scan. Long enough that
    # holding a card against the reader is one scan, short enough that
    # someone refused for a missing hardhat can fix it and re-present the
    # same badge without waiting.
    REPEAT_LOCKOUT_SECONDS = 3.0

    def _loop(self) -> None:
        last_tag = None
        last_at = 0.0

        while self._running:
            try:
                # Deliberately the non-blocking read. read() blocks until a
                # card appears, which would make stop() unable to end this
                # thread and would hold the SPI bus while GPIO.cleanup() runs.
                tag_id, _text = self._reader.read_no_block()
            except Exception:  # noqa: BLE001 - a bad read shouldn't kill the gate
                # Sleep on the error path too: a persistently failing bus
                # would otherwise spin this loop at full CPU.
                time.sleep(0.2)
                continue

            now = time.monotonic()

            if tag_id is None:
                # Card withdrawn — the next presentation counts as new even
                # if it's the same badge.
                last_tag = None
                time.sleep(0.08)
                continue

            tag = str(tag_id).strip()
            if not tag:
                time.sleep(0.08)
                continue

            # Dedupe on time, not identity alone. Keying only on "different
            # from last tag" means a badge can never be scanned twice in a
            # row — so a worker turned away, who then fixes their gear and
            # re-presents the same card, is met with silence.
            if tag != last_tag or now - last_at >= self.REPEAT_LOCKOUT_SECONDS:
                last_tag = tag
                last_at = now
                self.tags.put(tag)

            time.sleep(0.08)

    def stop(self) -> None:
        super().stop()
        try:
            import RPi.GPIO as GPIO

            GPIO.cleanup()
        except Exception:  # noqa: BLE001
            pass


class KeyboardReader(BadgeReader):
    """Development stand-in: read a tag from stdin.

    Also covers USB keyboard-wedge readers, which simply type the tag.
    """

    name = "keyboard (no reader detected)"

    def _loop(self) -> None:
        while self._running:
            try:
                line = sys.stdin.readline()
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            tag = line.strip()
            if tag:
                self.tags.put(tag)


def open_reader(alerts=None, policy_provider=None, readings=None) -> BadgeReader:
    """Return the best available reader.

    Order is serial → SPI → keyboard. The master ESP32 wins when it's
    attached because it carries hazard reports as well as badges, and only
    one process can hold that port open — so if a master is present, this is
    the thing that must own it.

    SAFETYFIRST_READER forces one: serial | mfrc522 | keyboard.
    """
    preference = os.environ.get("SAFETYFIRST_READER", "auto").lower()

    if preference in ("keyboard", "stdin"):
        return KeyboardReader()

    if preference in ("auto", "serial", "esp32"):
        from serial_bridge import SerialBridgeReader, find_port

        if preference != "auto" or find_port():
            try:
                return SerialBridgeReader(alerts=alerts, policy_provider=policy_provider,
                                          readings=readings)
            except Exception as exc:  # noqa: BLE001 - no pyserial, no port, busy
                if preference in ("serial", "esp32"):
                    raise SystemExit(f"Serial master unavailable: {exc}")
                print(f"[badge] serial master not available ({exc})")

    if preference in ("auto", "mfrc522"):
        try:
            reader = MFRC522Reader()
            return reader
        except Exception as exc:  # noqa: BLE001 - missing libs, no SPI, etc.
            if preference == "mfrc522":
                raise SystemExit(f"MFRC522 unavailable: {exc}")
            print(f"[badge] MFRC522 not available ({exc}); using keyboard input")

    return KeyboardReader()
