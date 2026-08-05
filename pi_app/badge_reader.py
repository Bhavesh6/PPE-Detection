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

    def _loop(self) -> None:
        last_tag = None
        while self._running:
            try:
                # Blocks until a card is present, so no busy-wait.
                tag_id, _text = self._reader.read()
            except Exception:  # noqa: BLE001 - a bad read shouldn't kill the gate
                continue

            tag = str(tag_id).strip()
            # A card held against the reader reads continuously; only emit on
            # a new card so one presentation isn't a dozen scans.
            if tag and tag != last_tag:
                last_tag = tag
                self.tags.put(tag)
            elif not tag:
                last_tag = None

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


def open_reader() -> BadgeReader:
    """Return the best available reader.

    SAFETYFIRST_READER=keyboard forces the fallback, which is useful when
    testing on a Pi that has the module attached.
    """
    preference = os.environ.get("SAFETYFIRST_READER", "auto").lower()

    if preference in ("keyboard", "stdin"):
        return KeyboardReader()

    if preference in ("auto", "mfrc522"):
        try:
            reader = MFRC522Reader()
            return reader
        except Exception as exc:  # noqa: BLE001 - missing libs, no SPI, etc.
            if preference == "mfrc522":
                raise SystemExit(f"MFRC522 unavailable: {exc}")
            print(f"[badge] MFRC522 not available ({exc}); using keyboard input")

    return KeyboardReader()
