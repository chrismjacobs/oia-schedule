# -*- coding: utf-8 -*-
"""Log in with a username instead of an email.

  * app_user.email is renamed to app_user.username
  * every value becomes the part before the @, lowercased
    ("Jian@oia.com" -> "jian") — usernames are case-insensitive

Checks first that no two accounts would end up with the same username, and
changes nothing if they would. Passwords are untouched here: the app accepts
an existing password as typed once and re-stores it case-insensitively (see
User.check_password).

Idempotent and transactional — safe to re-run, and safe to run against a
database that has already been migrated. Run it once per environment, together
with deploying the code that reads `username`:

    cd backend && venv/Scripts/python migrations/2026-09-11_username_login.py
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config import Config
from app.models import normalize_username


def main():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    insp = inspect(engine)

    if "app_user" not in insp.get_table_names():
        print("No app_user table — nothing to migrate (fresh database).")
        return

    cols = {c["name"] for c in insp.get_columns("app_user")}
    rename = "email" in cols and "username" not in cols
    col = "email" if rename else "username"
    did = []
    with engine.begin() as c:
        rows = c.execute(text(f"SELECT id, {col} FROM app_user ORDER BY id")).all()
        new = {uid: normalize_username(name.split("@")[0]) for uid, name in rows}

        # Checked before anything is written: SQLite commits DDL on the spot,
        # so a rename can't be relied on to roll back.
        by_name = defaultdict(list)
        for uid, name in new.items():
            by_name[name].append(uid)
        clashes = {name: ids for name, ids in by_name.items() if len(ids) > 1}
        if clashes:
            raise SystemExit("Aborted, nothing changed — these accounts would share a username: "
                             + "; ".join(f"{n!r}: user ids {ids}" for n, ids in clashes.items()))

        if rename:
            c.execute(text("ALTER TABLE app_user RENAME COLUMN email TO username"))
            did.append("app_user.email renamed to username")

        for uid, name in rows:
            if new[uid] != name:
                c.execute(text("UPDATE app_user SET username = :u WHERE id = :id"),
                          {"u": new[uid], "id": uid})
                did.append(f"user {uid}: {name} -> {new[uid]}")

    print("Migration applied." if did else "Nothing to do — already migrated.")
    for line in did:
        print("  -", line)


if __name__ == "__main__":
    main()
