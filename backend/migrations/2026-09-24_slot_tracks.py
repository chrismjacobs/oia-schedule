# -*- coding: utf-8 -*-
"""Worker lanes: adds `track` to slot, regular_slot and regular_slot_template.

An hour staffed by both a paid and an unpaid worker becomes two slot rows —
one per lane — rather than one slot holding two people. So each table's
uniqueness widens by one column and nothing else changes shape:

    slot                    (date, hour)     -> (date, hour, track)
    regular_slot            (date, hour)     -> (date, hour, track)
    regular_slot_template   (weekday, hour)  -> (weekday, hour, track)

`assignment` is deliberately NOT touched: its (schedule_id, slot_id) unique
constraint is what guarantees one student per slot, and that stays true.

Backward compatible. Every existing row gets track='OW', which is exactly how
the app behaved before, and every constraint change is a loosening — so the
currently-deployed code keeps working after this runs. That means it can go in
ahead of the deploy, with no downtime.

Idempotent — safe to re-run. Run it once per environment, before deploying the
code that reads the column:

    cd backend && venv/Scripts/python migrations/2026-09-24_slot_tracks.py

Pass --dry-run first. Postgres does DDL inside transactions, so that runs
every statement against the real schema and then rolls the whole thing back:
if it reports success, the same run without the flag will work, and if a
constraint name has drifted you find out with nothing changed.

    cd backend && venv/Scripts/python migrations/2026-09-24_slot_tracks.py --dry-run
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config import Config

# table -> (old constraint name, new constraint name, columns of the new one)
PLAN = {
    "slot": ("uq_slot_date_hour", "uq_slot_date_hour_track", ["date", "hour", "track"]),
    "regular_slot": ("uq_regular_slot_date_hour", "uq_regular_slot_date_hour_track",
                     ["date", "hour", "track"]),
    "regular_slot_template": ("uq_regular_template_weekday_hour",
                              "uq_regular_template_weekday_hour_track",
                              ["weekday", "hour", "track"]),
}


def main(dry_run=False):
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    insp = inspect(engine)
    tables = set(insp.get_table_names())

    missing = [t for t in PLAN if t not in tables]
    if len(missing) == len(PLAN):
        print("None of the slot tables exist — nothing to migrate (fresh database).")
        return

    is_sqlite = engine.dialect.name == "sqlite"
    if dry_run and is_sqlite:
        print("--dry-run needs transactional DDL, which SQLite does not have. "
              "Use a Postgres database, or just run it — local SQLite is disposable.")
        return

    done = []
    # One transaction for the whole migration. Postgres rolls DDL back like
    # anything else, so a dry run proves every statement against the real
    # schema and leaves nothing behind — and a failure halfway through a real
    # run undoes the earlier steps rather than leaving a half-migrated schema.
    conn = engine.connect()
    trans = conn.begin()
    try:
        for table, (old_uq, new_uq, cols) in PLAN.items():
            if table not in tables:
                continue

            if "track" not in {c["name"] for c in insp.get_columns(table)}:
                # NOT NULL with a default is metadata-only on Postgres 11+ —
                # no table rewrite, no meaningful lock.
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN track VARCHAR(2) NOT NULL DEFAULT 'OW'"))
                done.append(f"{table}.track added")

            existing = {u["name"] for u in insp.get_unique_constraints(table)}
            if new_uq in existing:
                continue

            if is_sqlite:
                # SQLite cannot drop a constraint; the table would have to be
                # rebuilt. Local SQLite databases are disposable, so say so
                # rather than doing surgery nobody needs.
                done.append(
                    f"{table}: track added, but the unique constraint still has the old shape. "
                    "On SQLite, delete backend/oia.db and re-seed to pick up the new schema.")
                continue

            if old_uq in existing:
                conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {old_uq}"))
            quoted = ", ".join(f'"{col}"' for col in cols)
            conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {new_uq} UNIQUE ({quoted})"))
            done.append(f"{table}: {old_uq} -> {new_uq}")

        if dry_run:
            trans.rollback()
        else:
            trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()

    if not done:
        print("Nothing to do — already migrated.")
        return
    print("Dry run OK — every statement ran, then was rolled back. Nothing changed."
          if dry_run else "Migration applied.")
    for line in done:
        print(f"  - {line}")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
