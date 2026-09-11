# -*- coding: utf-8 -*-
"""Student worker type: OW (Official Worker) / SW (Service Worker) /
TA (Teaching Assistant).

Adds a nullable `student.worker_type` — existing students start as "not set"
and the overseer fills it in from the dashboard.

Idempotent — safe to re-run, and safe to run against a database that has
already been migrated. Run it once per environment, before deploying the code
that reads the column:

    cd backend && venv/Scripts/python migrations/2026-09-11_student_worker_type.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config import Config


def main():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    insp = inspect(engine)

    if "student" not in insp.get_table_names():
        print("No student table — nothing to migrate (fresh database).")
        return

    if "worker_type" in {c["name"] for c in insp.get_columns("student")}:
        print("Nothing to do — already migrated.")
        return

    with engine.begin() as c:
        c.execute(text("ALTER TABLE student ADD COLUMN worker_type VARCHAR(2)"))
    print("Migration applied.\n  - student.worker_type added")


if __name__ == "__main__":
    main()
