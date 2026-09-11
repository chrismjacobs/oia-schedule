# -*- coding: utf-8 -*-
""""No hours this month": creates the availability_optout table.

A new, empty table — nothing existing is changed. Idempotent — safe to
re-run. Run it once per environment, before deploying the code that reads it:

    cd backend && venv/Scripts/python migrations/2026-09-11_availability_optout.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect

from app.config import Config
from app.models import AvailabilityOptOut


def main():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    if "availability_optout" in inspect(engine).get_table_names():
        print("Nothing to do — already migrated.")
        return
    AvailabilityOptOut.__table__.create(bind=engine)
    print("Migration applied.\n  - availability_optout created")


if __name__ == "__main__":
    main()
