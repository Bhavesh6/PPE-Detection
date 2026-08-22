"""Safety notices: a refusal handed to somebody outside this system.

Every other route here answers to an operator with a login, which is
correct for a console and useless for the one thing a refusal needs — the
supervisor or contractor who can actually fix it, and who will never hold
an account on our system.

So a notice travels by capability rather than identity. The link carries a
token that can do exactly three things: read this one notice, fetch the
evidence this one notice cites, and acknowledge it once. Everything else
on the API stays behind the same auth it had before.

That is a deliberate narrowing of a rule the rest of the codebase holds
firmly — admin.py serves a refusal's evidence behind @admin_required
precisely so somebody's face at a bad moment is not public. The exception
is bounded on every side that matters: one subject, one recipient, images
reachable only through the notice that cites them, an expiry, and no route
that lists or enumerates anything.
"""

import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, current_app, jsonify, request, send_file
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy.exc import IntegrityError

import audit
import evidence
from admin import admin_required
from extensions import db, limiter
from models import DetectionRecord, SafetyNotice, SafetyNoticeItem, User

notices_bp = Blueprint("notices", __name__)

# How long a recipient has before the notice reads as overdue. Long enough
# that a weekend does not fail somebody, short enough to still mean today's
# problem.
DEFAULT_DUE_DAYS = 3

# A link that outlives the thing it is about is a liability, not a feature.
#
# Retention is counted from the refusal; a link's life is counted from the
# notice. Those are different clocks, and treating them as one was wrong:
# a 28-day-old refusal cited today would leave the link valid for 28 days
# after its own evidence had been purged, so the recipient opens a notice
# with nothing in it. The expiry below is therefore the earlier of the two.
LINK_LIFETIME_DAYS = 30


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_reference():
    """SN-YYYY-NNNN, restarting each year.

    Sequential rather than random because a reference exists to be quoted
    in an email or read down a phone, and "SN-2026-0007" survives that
    where a token does not.
    """
    year = _now().year
    prefix = f"SN-{year}-"
    last = (SafetyNotice.query
            .filter(SafetyNotice.reference.like(f"{prefix}%"))
            .order_by(SafetyNotice.reference.desc())
            .first())
    nth = 1
    if last is not None:
        try:
            nth = int(last.reference.rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            nth = 1
    return f"{prefix}{nth:04d}"


def _expires_at(notice):
    """The earlier of: the link's own life, and its evidence's.

    Returns None when nothing it cites carries an image, in which case
    there is no proof to outlive and only the link's own limit applies.
    """
    own = notice.issued_at + timedelta(days=LINK_LIFETIME_DAYS)

    retention = current_app.config.get("EVIDENCE_RETENTION_DAYS", 30)
    if retention and retention > 0:
        shots = [i.detection.timestamp for i in notice.items
                 if i.detection is not None and i.detection.evidence_file]
        if shots:
            # Oldest cited refusal decides: once its image goes, the
            # notice is no longer showing what it claims to show.
            own = min(own, min(shots) + timedelta(days=retention))
    return own


def _expired(notice):
    return _now() > _expires_at(notice)


def issue(detection_ids, recipient_name, recipient_org=None,
          recipient_email=None, message=None, due_days=DEFAULT_DUE_DAYS,
          actor=None):
    """Create a notice citing existing refusals. Returns (notice, error)."""
    name = (recipient_name or "").strip()
    if not name:
        return None, "A recipient name is required"

    try:
        ids = [int(i) for i in (detection_ids or [])]
    except (TypeError, ValueError):
        return None, "Refusal ids must be numbers"
    if not ids:
        return None, "A notice must cite at least one refusal"

    records = DetectionRecord.query.filter(DetectionRecord.id.in_(ids)).all()
    found = {r.id for r in records}
    missing = [i for i in ids if i not in found]
    if missing:
        return None, f"No such refusal: {', '.join(str(i) for i in missing)}"

    # A notice that mixed people would be unanswerable: the recipient could
    # not act on it, and neither party could tell whose record it settled.
    subjects = {r.user_id for r in records}
    if len(subjects) > 1:
        return None, "A notice covers one worker; these refusals span several"

    not_denied = [r.id for r in records if r.verdict != "denied"]
    if not_denied:
        return None, (f"Only refusals can be cited; "
                      f"{', '.join(str(i) for i in not_denied)} was not one")

    subject_id = records[0].user_id
    if db.session.get(User, subject_id) is None:
        return None, "The worker on these refusals no longer exists"

    try:
        days = max(1, min(60, int(due_days)))
    except (TypeError, ValueError):
        days = DEFAULT_DUE_DAYS

    notice = SafetyNotice(
        reference=_next_reference(),
        token=secrets.token_urlsafe(32),
        subject_user_id=subject_id,
        recipient_name=name[:120],
        recipient_org=(recipient_org or "").strip()[:120] or None,
        recipient_email=(recipient_email or "").strip()[:255] or None,
        message=(message or "").strip() or None,
        issued_at=_now(),
        issued_by_id=getattr(actor, "id", None),
        issued_by_name=getattr(actor, "name", "Unknown")[:120],
        due_at=_now() + timedelta(days=days),
    )
    # References are sequential so they can be quoted, which means two
    # officers issuing at the same moment can compute the same one. Seen
    # under test: three of five concurrent issues died on the unique
    # constraint and the officer got a 500 with no notice created. Retry
    # rather than serialise - the collision is rare and the recovery is
    # cheap.
    for attempt in range(5):
        try:
            db.session.add(notice)
            db.session.flush()      # need notice.id for the citations
            for record in records:
                db.session.add(SafetyNoticeItem(notice_id=notice.id,
                                                detection_id=record.id))
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
            if attempt == 4:
                return None, "Could not allocate a reference; please try again"
            notice.reference = _next_reference()

    audit.record(
        "notice.issue",
        f"Issued {notice.reference} to {notice.recipient_name}",
        detail={"reference": notice.reference,
                "refusals": sorted(found),
                "recipient": notice.recipient_name},
        actor=actor,
    )
    return notice, None


def by_token(token, mark_delivered=False):
    """The notice a link opens, or None. Records first open as delivery."""
    token = (token or "").strip()
    if not token:
        return None
    notice = SafetyNotice.query.filter_by(token=token).first()
    if notice is None or notice.revoked_at is not None or _expired(notice):
        return None
    if mark_delivered and notice.delivered_at is None:
        notice.delivered_at = _now()
        db.session.commit()
    return notice


def acknowledge(notice, name, corrective_action=None, outcome="accepted"):
    """Record the recipient's answer. Returns (notice, error).

    Two answers are possible, and both close the loop. Accepting says what
    was done about it. Disputing says the refusal itself was wrong - a
    false positive, the wrong policy, the wrong person - which a detector
    that can be mistaken has to allow for. Recording only agreement would
    have made assent the sole reply the system could represent.

    Deliberately once. A second answer would overwrite who answered and
    when, which is the part a disagreement turns on.
    """
    if notice.acknowledged_at is not None:
        return None, "This notice has already been answered"
    if notice.revoked_at is not None:
        return None, "This notice was withdrawn"

    who = (name or "").strip()
    if not who:
        return None, "Please give your name"

    if outcome not in ("accepted", "disputed"):
        return None, "Answer must be accepted or disputed"

    note = (corrective_action or "").strip()
    if outcome == "disputed" and not note:
        # An unexplained dispute cannot be acted on by anyone.
        return None, "Please say why you disagree"

    notice.acknowledged_at = _now()
    notice.acknowledged_by = who[:120]
    notice.corrective_action = note or None
    notice.outcome = outcome
    db.session.commit()

    audit.record(
        f"notice.{notice.outcome}",
        f"{notice.reference} {notice.outcome} by {notice.acknowledged_by}",
        detail={"reference": notice.reference,
                "outcome": notice.outcome,
                "note": notice.corrective_action},
        # Named rather than looked up: the recipient holds no account, and
        # there is no JWT on this request to read one from.
        actor_name=notice.acknowledged_by,
    )
    return notice, None


def revoke(notice, actor=None):
    """Withdraw a notice. Returns (notice, error).

    A link sent to the wrong address cannot be unsent, so the only thing
    that can be withdrawn is its power to open. Without this the sole
    remedy for a misaddressed notice was to wait thirty days.

    An acknowledged notice is left alone: it is the record of an exchange
    that happened, and deleting the answer to a question is worse than
    having asked it badly.
    """
    if notice.acknowledged_at is not None:
        return None, "This notice has been answered; it cannot be withdrawn"
    if notice.revoked_at is not None:
        return None, "This notice was already withdrawn"

    notice.revoked_at = _now()
    db.session.commit()

    audit.record(
        "notice.revoke",
        f"Withdrew {notice.reference}",
        detail={"reference": notice.reference,
                "recipient": notice.recipient_name},
        actor=actor,
    )
    return notice, None


# ---------------------------------------------------------------------
# Console side
# ---------------------------------------------------------------------

@notices_bp.route("/api/admin/notices", methods=["GET"])
@admin_required
def list_notices():
    """Outstanding first: the ones that need chasing are the point."""
    rows = SafetyNotice.query.order_by(SafetyNotice.issued_at.desc()).all()
    order = {"disputed": 0, "overdue": 1, "issued": 2, "opened": 3,
             "acknowledged": 4, "withdrawn": 5}
    rows.sort(key=lambda n: order.get(n.status, 9))
    return jsonify({
        "success": True,
        "notices": [n.to_dict() for n in rows],
        # A dispute is answered but unresolved, so it still counts as
        # something waiting on a person.
        "outstanding": sum(1 for n in rows
                           if n.status not in ("acknowledged", "withdrawn")),
    })


@notices_bp.route("/api/admin/notices", methods=["POST"])
@admin_required
@limiter.limit("30 per hour")
def create_notice():
    actor = db.session.get(User, int(get_jwt_identity()))
    data = request.get_json(silent=True) or {}

    notice, error = issue(
        data.get("refusals"),
        data.get("recipient_name"),
        recipient_org=data.get("recipient_org"),
        recipient_email=data.get("recipient_email"),
        message=data.get("message"),
        due_days=data.get("due_days", DEFAULT_DUE_DAYS),
        actor=actor,
    )
    if error:
        return jsonify({"success": False, "message": error}), 400

    return jsonify({
        "success": True,
        "notice": notice.to_dict(),
        # The whole point of the feature: something to send.
        "link": f"/notice.html?t={notice.token}",
    }), 201


@notices_bp.route("/api/admin/notices/<reference>/revoke", methods=["POST"])
@admin_required
def revoke_notice(reference):
    actor = db.session.get(User, int(get_jwt_identity()))
    notice = SafetyNotice.query.filter_by(reference=reference).first()
    if notice is None:
        return jsonify({"success": False, "message": "No such notice"}), 404
    notice, error = revoke(notice, actor=actor)
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "notice": notice.to_dict()})


@notices_bp.route("/api/admin/notices/<reference>.json", methods=["GET"])
@admin_required
def export_json(reference):
    """One notice, in a shape another system can read.

    Same fields the console renders, so a contractor's own tooling and our
    page cannot drift into disagreeing about what was sent.
    """
    notice = SafetyNotice.query.filter_by(reference=reference).first()
    if notice is None:
        return jsonify({"success": False, "message": "No such notice"}), 404
    payload = notice.to_dict()
    # Consumers parse this. Naming the shape means a later change can be
    # detected rather than silently misread as the old one.
    payload = {"schema": "safetyfirst.notice/1", **payload}
    body = json.dumps(payload, indent=2)
    return Response(body, mimetype="application/json", headers={
        "Content-Disposition": f'attachment; filename="{reference}.json"',
    })


@notices_bp.route("/api/admin/notices.csv", methods=["GET"])
@admin_required
def export_csv():
    """Every notice, one row each, for a spreadsheet or an audit pack."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Reference", "Status", "Worker", "Employee ID", "Recipient",
        "Organisation", "Email", "Refusals cited", "Issued", "Issued by",
        "Due", "Opened", "Acknowledged", "Acknowledged by",
        "Corrective action",
    ])
    for n in SafetyNotice.query.order_by(SafetyNotice.issued_at.desc()).all():
        d = n.to_dict()
        writer.writerow([
            d["reference"], d["status"], d["subject"]["name"],
            d["subject"]["employee_id"], d["recipient"]["name"],
            d["recipient"]["organisation"], d["recipient"]["email"],
            len(d["refusals"]), d["issued_at"], d["issued_by"], d["due_at"],
            d["delivered_at"], d["acknowledged_at"], d["acknowledged_by"],
            d["corrective_action"],
        ])
    stamp = _now().strftime("%Y-%m-%d")
    return Response(buf.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="safety-notices-{stamp}.csv"',
    })


# ---------------------------------------------------------------------
# The worker the notice is about
# ---------------------------------------------------------------------

@notices_bp.route("/api/notices/me", methods=["GET"])
@jwt_required()
def my_notices():
    """Notices issued about the signed-in worker.

    Their name, employee id and a photograph of their face go to somebody
    outside this system, and until this existed they had no way to know it
    had happened. Somebody who cannot see what was said about them cannot
    correct it, and a safety record that the worker cannot inspect is one
    they cannot defend themselves against.

    The token is not in to_dict(), so this shows what was sent and to whom
    without handing over the ability to answer on the recipient's behalf.
    """
    rows = (SafetyNotice.query
            .filter_by(subject_user_id=int(get_jwt_identity()))
            .order_by(SafetyNotice.issued_at.desc())
            .all())
    return jsonify({
        "success": True,
        "notices": [n.to_dict() for n in rows],
    })


# ---------------------------------------------------------------------
# Recipient side — no account, no session, one token
# ---------------------------------------------------------------------

@notices_bp.route("/api/notice/<token>", methods=["GET"])
@limiter.limit("60 per hour")
def read_notice(token):
    notice = by_token(token, mark_delivered=True)
    if notice is None:
        # One message for missing, wrong and expired alike: a different
        # answer for each would turn this into an oracle for guessing.
        return jsonify({"success": False,
                        "message": "This notice is not available."}), 404
    return jsonify({"success": True, "notice": notice.to_dict()})


@notices_bp.route("/api/notice/<token>/evidence/<int:detection_id>", methods=["GET"])
@limiter.limit("60 per hour")
def notice_evidence(token, detection_id):
    """The frame behind one cited refusal.

    Scoped twice: the token must open a notice, and the notice must cite
    this exact refusal. Nothing here can reach a record the recipient was
    not already shown.
    """
    notice = by_token(token)
    if notice is None:
        return jsonify({"success": False,
                        "message": "This notice is not available."}), 404

    cited = any(item.detection_id == detection_id for item in notice.items)
    if not cited:
        return jsonify({"success": False,
                        "message": "Not part of this notice."}), 404

    record = db.session.get(DetectionRecord, detection_id)
    if record is None or not record.evidence_file:
        return jsonify({"success": False,
                        "message": "No evidence stored for this refusal."}), 404

    path = evidence.path_for(record.evidence_file)
    if path is None:
        return jsonify({"success": False,
                        "message": "The image is past its retention window."}), 404
    return send_file(path, mimetype="image/jpeg")


@notices_bp.route("/api/notice/<token>/acknowledge", methods=["POST"])
@limiter.limit("20 per hour")
def acknowledge_notice(token):
    notice = by_token(token, mark_delivered=True)
    if notice is None:
        return jsonify({"success": False,
                        "message": "This notice is not available."}), 404

    data = request.get_json(silent=True) or {}
    notice, error = acknowledge(notice, data.get("name"),
                                data.get("corrective_action"),
                                outcome=data.get("outcome", "accepted"))
    if error:
        return jsonify({"success": False, "message": error}), 400
    return jsonify({"success": True, "notice": notice.to_dict()})
