"""One-off CLI to flag an existing user as admin.

Usage (from backend/, with the venv active):
    python make_admin.py you@example.com
"""
import sys

from app import app
from extensions import db
from models import User

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python make_admin.py <email>")
        sys.exit(1)

    email = sys.argv[1].strip().lower()

    with app.app_context():
        user = User.query.filter_by(email=email).first()
        if user is None:
            print(f"No user found with email {email}")
            sys.exit(1)

        user.is_admin = True
        db.session.commit()
        print(f"{user.name} <{user.email}> is now an admin.")
