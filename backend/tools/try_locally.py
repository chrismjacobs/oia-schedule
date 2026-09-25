# -*- coding: utf-8 -*-
"""Run the app on a throwaway local database, with demo data, to try changes.

    cd backend
    venv/Scripts/python tools/try_locally.py

Why a script rather than "set DATABASE_URL and run wsgi.py": .env points at
the live Neon database, so the one step you must not fumble is the one that
decides which database you are about to click around in. This sets it before
anything reads it, and refuses to start if it somehow still resolves to
anything but its own local file.

The database lives at backend/oia-local.db and is rebuilt on every run, so
nothing here is worth keeping and nothing here can reach the real roster.
Delete it whenever you like.

    --keep    reuse the existing local database instead of rebuilding it
    --port N  serve on a different port (default 5057)
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

DB_PATH = os.path.join(BACKEND, "oia-local.db").replace("\\", "/")

# Set before importing anything from app: app.config resolves the database URL
# once, at import time, and keeps it.
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH
os.environ["FLASK_DEBUG"] = "1"          # also lets the session cookie work over plain http
os.environ["ALLOW_LIVE_NOTIFICATIONS"] = "0"   # nothing reaches the student LINE group

ADMIN_USER = "test"
ADMIN_PASS = "test1234"


def main():
    keep = "--keep" in sys.argv
    port = 5057
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])

    from app.config import Config
    if not Config.SQLALCHEMY_DATABASE_URI.startswith("sqlite:///"):
        sys.exit(f"Refusing to start: resolved to {Config.SQLALCHEMY_DATABASE_URI}, "
                 "which is not the local throwaway database.")

    if not keep and os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    from app import create_app
    from app.extensions import db
    from app.models import User

    app = create_app()
    with app.app_context():
        db.create_all()
        if not keep:
            from app.admin.demo import seed_demo
            result = seed_demo()
            print(f"\nSeeded demo month {result['month']['year_month']}: "
                  f"{result['students']} students, "
                  f"{result['meta']['assigned']}/{result['meta']['total_slots']} hours filled.")
        if not User.query.filter_by(username=ADMIN_USER).first():
            u = User(username=ADMIN_USER, role="overseer")
            u.set_password(ADMIN_PASS)
            db.session.add(u)
            db.session.commit()

    print(f"""
Local database: {DB_PATH}
Nothing here touches the live roster.

    http://127.0.0.1:{port}/login
    username  {ADMIN_USER}
    password  {ADMIN_PASS}

Ctrl-C to stop.
""")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
