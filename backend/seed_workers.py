"""Create demo workers with badge IDs, so the gate can be tested without
a card reader attached.

    python seed_workers.py

The tags below are what a keyboard-wedge RFID reader would "type". Type one
into the checkpoint app and press Enter to simulate a scan.
"""

from app import app
from extensions import db
from models import User

WORKERS = [
    {
        "name": "Bhavesh Waghmare",
        "email": "bhavesh@site.local",
        "employee_id": "EMP-0431",
        "rfid_tag": "0006238412",
        "age": 21,
        "role": "Site Engineer",
    },
    {
        "name": "Jasbir Singh Monga",
        "email": "jasbir@site.local",
        "employee_id": "EMP-0088",
        "rfid_tag": "0009471255",
        "age": 24,
        "role": "Safety Officer",
    },
    {
        "name": "Priyal Vairagade",
        "email": "priyal@site.local",
        "employee_id": "EMP-0192",
        "rfid_tag": "0003118907",
        "age": 22,
        "role": "Structural Trainee",
    },
]


def main():
    with app.app_context():
        for spec in WORKERS:
            user = User.query.filter_by(email=spec["email"]).first()
            if user is None:
                user = User(name=spec["name"], email=spec["email"])
                user.set_password("worker1234")
                db.session.add(user)

            user.name = spec["name"]
            user.employee_id = spec["employee_id"]
            user.rfid_tag = spec["rfid_tag"]
            user.age = spec["age"]
            user.role = spec["role"]

        db.session.commit()

        print("Badges ready — type one at the checkpoint and press Enter:\n")
        for spec in WORKERS:
            print(f"  {spec['rfid_tag']}   {spec['name']:22} {spec['role']}")
        print("\nAll seeded accounts use the password: worker1234")


if __name__ == "__main__":
    main()
