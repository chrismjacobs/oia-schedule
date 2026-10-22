# -*- coding: utf-8 -*-
"""Relabel stale "no sign-out" flags on sessions that were in fact signed out.

/tick flags a session `forgot_sign_out` once its run has ended plus grace. If
the student then signed out the same day, sign-out used to leave that flag in
place, so history showed a sign-out time next to "no sign-out". Sign-out now
turns it into `late_sign_out`; this fixes the rows written before that.

Data only, no schema change. Idempotent — safe to re-run:

    cd backend && venv/Scripts/python migrations/2026-10-22_stale_forgot_signout_flags.py --dry-run
    cd backend && venv/Scripts/python migrations/2026-10-22_stale_forgot_signout_flags.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from app.config import Config

WHERE = "flag_reason = 'forgot_sign_out' AND signed_out_at IS NOT NULL"


def main():
    dry_run = "--dry-run" in sys.argv
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    with engine.begin() as conn:
        rows = conn.execute(text(
            f"SELECT id, student_id, date, signed_out_at FROM attendance_session WHERE {WHERE}"
        )).fetchall()
        for r in rows:
            print(f"session #{r.id} student={r.student_id} {r.date} signed out {r.signed_out_at}")
        if dry_run:
            print(f"{len(rows)} session(s) would be relabelled late_sign_out (dry run, nothing changed).")
            return
        conn.execute(text(f"UPDATE attendance_session SET flag_reason = 'late_sign_out' WHERE {WHERE}"))
        print(f"{len(rows)} session(s) relabelled late_sign_out.")


if __name__ == "__main__":
    main()
