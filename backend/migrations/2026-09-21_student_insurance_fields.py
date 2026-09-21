# -*- coding: utf-8 -*-
"""Student project name and funding category — the two fields the university's
insurance portal asks for that we weren't holding yet.

Adds nullable `student.project_name` (free text) and
`student.funding_category` (the portal's own ddlJobCategory option value,
"0".."6"). Existing students start as "not set" and the overseer fills them in
from the dashboard — no existing student data is touched.

Idempotent — safe to re-run, and safe against a database that has already been
migrated. It adds only the columns that are missing, so a half-applied run
finishes cleanly. Run it once per environment, before deploying the code that
reads the columns:

    cd backend && venv/Scripts/python migrations/2026-09-21_student_insurance_fields.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config import Config

COLUMNS = {
    "project_name": "VARCHAR(128)",
    "funding_category": "VARCHAR(1)",
}


def main():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    insp = inspect(engine)

    if "student" not in insp.get_table_names():
        print("No student table — nothing to migrate (fresh database).")
        return

    existing = {c["name"] for c in insp.get_columns("student")}
    missing = {name: ddl for name, ddl in COLUMNS.items() if name not in existing}
    if not missing:
        print("Nothing to do — already migrated.")
        return

    with engine.begin() as c:
        for name, ddl in missing.items():
            c.execute(text(f"ALTER TABLE student ADD COLUMN {name} {ddl}"))
    print("Migration applied.")
    for name in missing:
        print(f"  - student.{name} added")


if __name__ == "__main__":
    main()
