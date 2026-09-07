# -*- coding: utf-8 -*-
"""One report per session, not per hour (CLAUDE.md #9).

Moves the sign-out write-up and the task ticks from the hour to the session:

  * attendance_session gains `note` — the whole session's write-up
  * hourly_report  ->  session_hour  (pure coverage: session_id + slot_id)
  * task_completion loses hourly_report_id / slot_id (it already has session_id)
  * custom_task    swaps hourly_report_id for session_id

Existing per-hour notes are folded into their session's note rather than
dropped, and every hourly_report row becomes the session_hour row for the same
slot, so recorded hours (and therefore the scheduled-vs-recorded gap) come
through unchanged.

Idempotent and transactional — safe to re-run, and safe to run against a
database that has already been migrated. Run it once per environment:

    cd backend && venv/Scripts/python migrations/2026-09-07_session_report.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text

from app.config import Config
from app.models import SessionHour


def column_names(insp, table):
    return {c["name"] for c in insp.get_columns(table)}


def main():
    url = Config.SQLALCHEMY_DATABASE_URI
    engine = create_engine(url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    did = []

    if "attendance_session" not in tables:
        print("No attendance_session table — nothing to migrate (fresh database).")
        return

    with engine.begin() as c:
        # 1. the session's own report
        if "note" not in column_names(insp, "attendance_session"):
            c.execute(text("ALTER TABLE attendance_session ADD COLUMN note TEXT"))
            did.append("attendance_session.note added")

        # 2. the coverage table
        if "session_hour" not in tables:
            SessionHour.__table__.create(bind=c)
            did.append("session_hour created")

        if "hourly_report" in tables:
            # 2a. fold each session's per-hour notes into one session note,
            #     in hour order, dropping empties and exact repeats (the old
            #     UI wrote the same line against every hour of a run).
            c.execute(text("""
                UPDATE attendance_session AS s
                   SET note = sub.note
                  FROM (
                        SELECT hr.session_id,
                               string_agg(DISTINCT btrim(hr.note), E'\\n') AS note
                          FROM hourly_report hr
                         WHERE hr.note IS NOT NULL AND btrim(hr.note) <> ''
                      GROUP BY hr.session_id
                       ) AS sub
                 WHERE s.id = sub.session_id
                   AND (s.note IS NULL OR btrim(s.note) = '')
            """))
            # 2b. every reported hour becomes a recorded hour
            moved = c.execute(text("""
                INSERT INTO session_hour (session_id, slot_id)
                     SELECT hr.session_id, hr.slot_id
                       FROM hourly_report hr
                  LEFT JOIN session_hour sh
                         ON sh.session_id = hr.session_id AND sh.slot_id = hr.slot_id
                      WHERE sh.id IS NULL
            """)).rowcount
            did.append(f"hourly_report rows carried over: {moved}")

        # 3. custom_task claims move from the hour to the session
        custom_cols = column_names(insp, "custom_task")
        if "session_id" not in custom_cols:
            c.execute(text(
                "ALTER TABLE custom_task ADD COLUMN session_id INTEGER "
                "REFERENCES attendance_session(id)"))
            did.append("custom_task.session_id added")
        if "hourly_report_id" in custom_cols:
            if "hourly_report" in tables:
                c.execute(text("""
                    UPDATE custom_task AS ct
                       SET session_id = hr.session_id
                      FROM hourly_report hr
                     WHERE hr.id = ct.hourly_report_id AND ct.session_id IS NULL
                """))
            c.execute(text("ALTER TABLE custom_task DROP COLUMN hourly_report_id"))
            did.append("custom_task.hourly_report_id dropped")

        # 4. task_completion already carries session_id — the hour columns go
        tc_cols = column_names(insp, "task_completion")
        for col in ("hourly_report_id", "slot_id"):
            if col in tc_cols:
                c.execute(text(f"ALTER TABLE task_completion DROP COLUMN {col}"))
                did.append(f"task_completion.{col} dropped")

        # 5. the old table, now fully superseded
        if "hourly_report" in tables:
            c.execute(text("DROP TABLE hourly_report"))
            did.append("hourly_report dropped")

    print("Migration applied." if did else "Nothing to do — already migrated.")
    for line in did:
        print("  -", line)


if __name__ == "__main__":
    main()
