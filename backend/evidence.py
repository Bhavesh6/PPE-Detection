"""Storage for refusal evidence frames.

A refusal that only says "Hardhat missing" asks a supervisor to take the
system's word for it. Keeping the frame that produced the decision turns
the log into something reviewable — and something a worker can contest.

Only refusals are kept. A grant has nothing to answer for, and
photographing everyone who passes would turn a safety gate into
surveillance.
"""

import os
import re
import time
from datetime import datetime, timedelta, timezone

import cv2
from flask import current_app

_SAFE_NAME = re.compile(r"^[0-9a-f\-]+\.jpg$")


def _dir():
    path = current_app.config["EVIDENCE_DIR"]
    os.makedirs(path, exist_ok=True)
    return path


def save(frame, record_id):
    """Write the frame that caused a refusal. Returns a filename or None.

    Failure to store evidence must never fail the gate decision — the
    decision is the safety-critical part; the image is documentation.
    """
    if frame is None:
        return None
    try:
        name = f"{record_id:08d}-{int(time.time())}.jpg"
        path = os.path.join(_dir(), name)
        # 80 keeps faces and PPE legible at roughly a quarter of the bytes
        # of default quality, which matters when these accumulate per refusal.
        ok = cv2.imwrite(path, frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        return name if ok else None
    except Exception:
        current_app.logger.exception("Could not store evidence frame")
        return None


def path_for(filename):
    """Resolve a stored filename to a full path, or None if it's unsafe.

    The filename reaches here from a database row, but the row is only as
    trustworthy as whatever wrote it — this is the last point where a
    "../../etc/passwd" would still be caught before an open().
    """
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    path = os.path.join(_dir(), filename)
    return path if os.path.isfile(path) else None


def purge_expired(session, model):
    """Delete evidence images past the retention window.

    Clears the column as well as the file, so the UI stops offering an
    image that no longer exists rather than showing a broken frame.
    Returns the number of images removed.
    """
    days = current_app.config["EVIDENCE_RETENTION_DAYS"]
    if days <= 0:
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
    stale = model.query.filter(
        model.evidence_file.isnot(None),
        model.timestamp < cutoff,
    ).all()

    removed = 0
    for record in stale:
        target = path_for(record.evidence_file)
        if target:
            try:
                os.remove(target)
                removed += 1
            except OSError:
                current_app.logger.warning("Could not delete %s", target)
        record.evidence_file = None

    if stale:
        session.commit()
    return removed
