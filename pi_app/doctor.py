"""Pre-flight check for the checkpoint device.

Run this on the Pi before trusting the gate - it walks the chain from
kernel to badge to backend and says which link is broken, rather than
leaving you to infer it from a blank screen at demo time.

    python doctor.py            # everything except the card read
    python doctor.py --scan     # also waits for a badge to be presented
"""

from __future__ import annotations

import glob
import os
import sys

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "
_failures = 0
_warnings = 0


def report(status, title, detail=""):
    global _failures, _warnings
    if status is BAD:
        _failures += 1
    elif status is WARN:
        _warnings += 1
    print(f"[{status}] {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"         {line}")


def check_platform():
    model = "unknown"
    try:
        with open("/proc/device-tree/model") as f:
            model = f.read().strip("\x00").strip()
    except OSError:
        pass

    if "Raspberry Pi" in model:
        report(OK, "Hardware", model)
    else:
        report(WARN, "Hardware", f"Not a Raspberry Pi ({model}).\n"
                                 "SPI and GPIO checks below will fail; that's expected off-device.")


def check_spi():
    devices = sorted(glob.glob("/dev/spidev*"))
    if devices:
        report(OK, "SPI enabled", ", ".join(devices))
        return True
    report(BAD, "SPI enabled",
           "No /dev/spidev* device.\n"
           "Enable it: sudo raspi-config -> Interface Options -> SPI -> Yes, then reboot.")
    return False


def check_libraries():
    missing = []
    for module, package in (("spidev", "spidev"),
                            ("RPi.GPIO", "RPi.GPIO"),
                            ("mfrc522", "mfrc522")):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    if missing:
        report(BAD, "Reader libraries",
               f"Missing: {', '.join(missing)}\n"
               f"Install: pip install {' '.join(missing)}")
        return False
    report(OK, "Reader libraries", "spidev, RPi.GPIO, mfrc522")
    return True


def check_camera():
    try:
        import cv2
    except ImportError:
        report(BAD, "Camera", "opencv is not installed (pip install opencv-python-headless)")
        return

    index = int(os.environ.get("SAFETYFIRST_CAMERA", "0"))
    cap = cv2.VideoCapture(index)
    try:
        if not cap.isOpened():
            report(BAD, "Camera", f"Could not open camera index {index}.\n"
                                  "Check the ribbon/USB connection, or set SAFETYFIRST_CAMERA.")
            return
        ok, frame = cap.read()
        if not ok or frame is None:
            report(BAD, "Camera", "Camera opened but returned no frame.")
            return
        h, w = frame.shape[:2]
        report(OK, "Camera", f"index {index}, {w}x{h}")
    finally:
        cap.release()


def check_backend():
    try:
        import requests
    except ImportError:
        report(BAD, "Backend", "requests is not installed (pip install requests)")
        return None

    base = os.environ.get("SAFETYFIRST_API", "http://localhost:5000")
    try:
        res = requests.get(f"{base}/api/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        report(BAD, "Backend reachable",
               f"{base} did not respond ({exc.__class__.__name__}).\n"
               "Start it, or point SAFETYFIRST_API at the machine running it.")
        return None

    if res.status_code != 200:
        report(BAD, "Backend reachable", f"{base} returned HTTP {res.status_code}")
        return None

    report(OK, "Backend reachable", base)
    return base


def check_credentials(base):
    if not base:
        return None
    import requests

    email = os.environ.get("SAFETYFIRST_EMAIL", "")
    password = os.environ.get("SAFETYFIRST_PASSWORD", "")
    if not email or not password:
        report(WARN, "Device credentials",
               "SAFETYFIRST_EMAIL / SAFETYFIRST_PASSWORD are unset.\n"
               "The gate signs in as a guest, which cannot record attendance.")
        return None

    try:
        res = requests.post(f"{base}/api/auth/login",
                            json={"email": email, "password": password}, timeout=5)
        data = res.json()
    except Exception as exc:  # noqa: BLE001
        report(BAD, "Device credentials", f"Sign-in failed ({exc})")
        return None

    if res.status_code != 200 or not data.get("success"):
        report(BAD, "Device credentials", data.get("message", "Sign-in rejected"))
        return None

    report(OK, "Device credentials", f"signed in as {email}")
    return data["token"]


def check_policy(base, token):
    if not base or not token:
        return
    import requests

    try:
        res = requests.get(f"{base}/api/status",
                           headers={"Authorization": f"Bearer {token}"}, timeout=5)
        required = res.json().get("required_ppe", [])
    except Exception as exc:  # noqa: BLE001
        report(WARN, "Site policy", f"Could not read it ({exc})")
        return

    if required:
        report(OK, "Site policy", "requires " + ", ".join(required))
    else:
        report(WARN, "Site policy", "No equipment is required - the gate will admit everyone.")


def check_reader(scan):
    try:
        import badge_reader
    except ImportError as exc:
        report(BAD, "Badge reader", str(exc))
        return

    try:
        reader = badge_reader.open_reader()
    except SystemExit as exc:
        report(BAD, "Badge reader", str(exc))
        return

    if isinstance(reader, badge_reader.KeyboardReader):
        report(WARN, "Badge reader",
               "Falling back to keyboard input - the RC522 was not detected.\n"
               "Check wiring: SDA->GPIO8, SCK->GPIO11, MOSI->GPIO10, MISO->GPIO9, RST->GPIO25, 3.3V (not 5V).")
        return

    report(OK, "Badge reader", reader.name)

    if not scan:
        print("         (run with --scan to test an actual card)")
        return

    import time
    print("\n         Present a badge to the reader (20s)...")
    reader.start()
    deadline = time.time() + 20
    tag = None
    while time.time() < deadline and tag is None:
        tag = reader.read()
        time.sleep(0.1)
    reader.stop()

    if tag:
        report(OK, "Badge scan", f"read tag {tag}")
        print("         Register it: set this as the worker's rfid_tag in the console.")
    else:
        report(BAD, "Badge scan", "No card seen in 20s. Check wiring and that the card is 13.56MHz (MIFARE).")


def main():
    scan = "--scan" in sys.argv
    print("\nSafetyFirst checkpoint pre-flight\n" + "=" * 40)

    check_platform()
    spi = check_spi()
    libs = check_libraries()
    check_camera()
    base = check_backend()
    token = check_credentials(base)
    check_policy(base, token)

    if spi and libs:
        check_reader(scan)
    else:
        report(WARN, "Badge reader", "Skipped - SPI or libraries unavailable.")

    print("=" * 40)
    if _failures:
        print(f"{_failures} problem(s) must be fixed before the gate is usable.")
    elif _warnings:
        print(f"Usable, with {_warnings} warning(s) above.")
    else:
        print("All checks passed - the checkpoint is ready.")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
