"""Assign a physical badge to a worker.

Until now the only tags in the database came from seed_workers.py, which
hardcodes three invented values — so no real card could ever open the gate.
This attaches a UID read from an actual reader to an actual worker.

    python enrol_badge.py --list
    python enrol_badge.py --user-id 1 --tag 0238731604

Get the UID by presenting the card at the gate: the master prints
`BADGE <uid>` on its serial line, and the Pi's log shows unknown scans.
Type it from that, not from the number printed on the card — the two are
usually different, and the reader's value is the one that matters.
"""

import argparse
import sys

from app import app
from extensions import db
from models import User


def main() -> int:
    ap = argparse.ArgumentParser(description="Assign a badge UID to a worker")
    ap.add_argument("--list", action="store_true", help="show workers and their badges")
    ap.add_argument("--user-id", type=int)
    ap.add_argument("--tag", help="UID as the reader reports it, e.g. 0238731604")
    ap.add_argument("--clear", action="store_true", help="remove the badge instead")
    args = ap.parse_args()

    with app.app_context():
        if args.list or not args.user_id:
            print(f"{'id':<5}{'name':<24}{'role':<18}badge")
            for user in User.query.filter(User.is_guest.is_(False)).order_by(User.id):
                print(f"{user.id:<5}{(user.name or ''):<24}"
                      f"{(user.role or ''):<18}{user.rfid_tag or '-'}")
            return 0

        user = db.session.get(User, args.user_id)
        if user is None:
            print(f"No worker with id {args.user_id}", file=sys.stderr)
            return 1

        if args.clear:
            previous, user.rfid_tag = user.rfid_tag, None
            db.session.commit()
            print(f"Cleared badge {previous} from {user.name}")
            return 0

        if not args.tag:
            print("--tag is required (or use --clear)", file=sys.stderr)
            return 1

        tag = args.tag.strip()

        # A badge that opens the gate for two people is worse than one that
        # opens it for nobody: the attendance record would name whichever
        # row the lookup happened to reach first.
        clash = User.query.filter(
            db.func.lower(User.rfid_tag) == tag.lower(),
            User.id != user.id,
        ).first()
        if clash:
            print(f"Refusing: {tag} is already assigned to {clash.name} "
                  f"(id {clash.id}). Clear it there first.", file=sys.stderr)
            return 1

        previous = user.rfid_tag
        user.rfid_tag = tag
        db.session.commit()
        print(f"{user.name}: {previous or '(none)'} -> {tag}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
