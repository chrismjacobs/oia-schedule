"""Bootstrap the very first overseer account from ADMIN_EMAIL/ADMIN_PASSWORD
in .env. Invite-only means every other account (overseer or student) is
created via /api/admin/invites by an existing overseer. Safe to re-run."""
from app import create_app
from app.extensions import db
from app.models import User

app = create_app()

with app.app_context():
    db.create_all()

    email = app.config["ADMIN_EMAIL"]
    password = app.config["ADMIN_PASSWORD"]
    if not email or not password:
        print("ADMIN_EMAIL / ADMIN_PASSWORD not set in .env — skipping overseer bootstrap")
    else:
        existing = User.query.filter(db.func.lower(User.email) == email.lower()).first()
        if existing:
            print(f"Overseer account already exists: {email}")
        else:
            user = User(email=email, role="overseer")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            print(f"Created overseer account: {email}")
