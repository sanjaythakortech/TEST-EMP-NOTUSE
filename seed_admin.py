"""Local-only interactive admin bootstrap.

For Vercel, do not run this file. Set ADMIN_* environment variables instead;
app.py creates the first admin automatically.
"""

from getpass import getpass

from app import app, db, User


with app.app_context():
    db.create_all()

    email = input("Admin email [admin@office.local]: ").strip().lower() or "admin@office.local"
    existing = User.query.filter_by(email=email).first()
    if existing:
        print("An account with that email already exists.")
        raise SystemExit(0)

    name = input("Admin name [Administrator]: ").strip() or "Administrator"
    employee_id = input("Admin Employee ID [ADM-1000]: ").strip() or "ADM-1000"

    if User.query.filter_by(employee_id=employee_id).first():
        raise SystemExit("That Employee ID already exists.")

    password = getpass("Admin password (min 6 chars): ")
    if len(password) < 6:
        raise SystemExit("Password must be at least 6 characters.")

    user = User(
        employee_id=employee_id,
        name=name,
        email=email,
        designation="Administrator",
        role="admin",
        email_verified=True,
        active=True,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    print(f"Admin created: {email} / {employee_id}")
