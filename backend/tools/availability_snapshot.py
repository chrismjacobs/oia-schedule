# -*- coding: utf-8 -*-
"""Prove no student selections were lost across a change.

"Did the migration eat October?" is not a question to answer by looking at a
grid and feeling reassured. Run this before, run it after, diff the two:

    cd backend
    venv/Scripts/python tools/availability_snapshot.py > before.json
    ... migrate, deploy ...
    venv/Scripts/python tools/availability_snapshot.py > after.json
    venv/Scripts/python tools/availability_snapshot.py --compare before.json

Read-only: it opens the database, counts, and writes nothing.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text

from app.config import Config

QUERY = """
SELECT m.year_month, s.student_id, s.english_name, sl.track, COUNT(*) AS hours
FROM availability a
JOIN slot sl ON sl.id = a.slot_id
JOIN month m ON m.id = sl.month_id
JOIN student s ON s.id = a.student_id
GROUP BY m.year_month, s.student_id, s.english_name, sl.track
ORDER BY m.year_month, s.student_id, sl.track
"""

# Before the migration there is no slot.track, so ask for it only if it exists.
QUERY_NO_TRACK = QUERY.replace(", sl.track", "").replace(", sl.track\n", "\n")


def snapshot():
    engine = create_engine(Config.SQLALCHEMY_DATABASE_URI)
    with engine.connect() as c:
        has_track = bool(c.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='slot' AND column_name='track'"
        )).first()) if engine.dialect.name != "sqlite" else "track" in {
            r[1] for r in c.execute(text("PRAGMA table_info(slot)"))
        }
        rows = c.execute(text(QUERY if has_track else QUERY_NO_TRACK)).mappings().all()

    out = {}
    for r in rows:
        key = f"{r['year_month']}|{r['student_id']}"
        out.setdefault(key, {"name": r["english_name"], "total": 0, "by_track": {}})
        out[key]["total"] += r["hours"]
        out[key]["by_track"][r.get("track") or "OW"] = r["hours"]
    return out


def compare(before_path):
    with open(before_path, encoding="utf-8") as fh:
        before = json.load(fh)
    after = snapshot()

    problems = []
    for key, was in sorted(before.items()):
        now = after.get(key)
        if now is None:
            problems.append(f"LOST  {key} ({was['name']}): {was['total']}h -> gone")
        elif now["total"] != was["total"]:
            word = "LOST " if now["total"] < was["total"] else "GAINED"
            problems.append(f"{word} {key} ({was['name']}): {was['total']}h -> {now['total']}h")
    for key in sorted(set(after) - set(before)):
        problems.append(f"NEW   {key} ({after[key]['name']}): {after[key]['total']}h")

    print(f"{len(before)} student-months before, {len(after)} after.")
    if not problems:
        print("OK - every student's hours are unchanged.")
        return 0
    for p in problems:
        print(p)
    return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", metavar="BEFORE.json",
                    help="diff the current state against an earlier snapshot")
    args = ap.parse_args()
    if args.compare:
        raise SystemExit(compare(args.compare))
    print(json.dumps(snapshot(), indent=2, ensure_ascii=False, sort_keys=True))
