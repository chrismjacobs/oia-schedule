"""Bootstrap the very first overseer account from ADMIN_USERNAME/ADMIN_PASSWORD
in .env. Invite-only means every other account (overseer or student) is
created via /api/admin/invites by an existing overseer. Safe to re-run."""
from app import create_app
from app.extensions import db
from app.models import User, normalize_username

app = create_app()

with app.app_context():
    db.create_all()

    username = normalize_username(app.config["ADMIN_USERNAME"])
    password = app.config["ADMIN_PASSWORD"]
    if not username or not password:
        print("ADMIN_USERNAME / ADMIN_PASSWORD not set in .env — skipping overseer bootstrap")
    else:
        existing = User.query.filter(db.func.lower(User.username) == username).first()
        if existing:
            print(f"Overseer account already exists: {username}")
        else:
            user = User(username=username, role="overseer")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            print(f"Created overseer account: {username}")
