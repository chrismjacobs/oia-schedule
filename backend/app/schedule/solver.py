"""OR-Tools CP-SAT allocator (CLAUDE.md #7). Deterministic, auditable,
config-driven. No LLM involved in allocation, ever.

It schedules sessions, not hours. A session is one student on one unbroken
run of hours inside a morning or an afternoon (never across lunch), between
the configured minimum and maximum length (2-4h by default). For every
student and half-day, every run they fully offered is a candidate; the
solver picks sessions, and a student's hours are simply the hours of the
sessions they were given. Deciding hour by hour, as this used to, let the
floor and equalising terms carve one morning into four single hours for four
different people — sessions make the shift the unit, which is also how
sign-in already works (one sign-in per run).

Hard constraints: at most one student per slot; never assign an hour a
student didn't offer (candidates are built only from offered hours); at most
one session per student per half-day. Closed dates and unavailable hours
have no slots, so they can't be scheduled.

Regular hours (`RegularSlot`, state=assigned) are honoured only if the
student offered that hour this month. They're a near-absolute bonus rather
than a carve-out, so the student's session is built around them — a regular
8-10 can grow into 8-12 if they offered it — and an odd combination of
regular hours can't make the whole month unsolvable.

Soft preferences, in priority order (weights in config.SOLVER_WEIGHTS, each
large enough to dominate everything below it):
  1. coverage         - fill as many hours as possible
  2. floor_guarantee  - everyone who offered enough gets >= floor hours
                        before anyone gets extra
  3. short_session    - a session under the minimum is a last resort, used
                        only to cover an hour nobody else can
  4. per_session      - fewer, longer sessions over the same hours
  5. same_day_double  - avoid giving one student both halves of a day
  6. low_churn        - repeat the same weekday/hour across consecutive weeks
                        (lets a 2-on/2-off rotation emerge on its own)
  7. equalise_hours   - narrow the gap between most and least scheduled

If CP-SAT can't produce a solution in the time budget, a session-aware
greedy round-robin takes over (CLAUDE.md #7: acceptable and explainable).
"""
from collections import defaultdict

from ortools.sat.python import cp_model

from app.models import Slot, Availability, RegularSlot

# Honouring a regular hour outranks everything else; kept out of the tunable
# weights on purpose — it's a standing arrangement, not a preference.
REGULAR_LOCK_FACTOR = 10


def _load_inputs(month_id):
    slots = Slot.query.filter_by(month_id=month_id).order_by(Slot.date, Slot.hour).all()
    offered = defaultdict(set)  # student_id -> {slot_id}
    for a in (Availability.query.join(Slot, Availability.slot_id == Slot.id)
              .filter(Slot.month_id == month_id).all()):
        offered[a.student_id].add(a.slot_id)
    return slots, offered


def _load_regular_locks(month_id, slots, offered):
    """{slot_id: student_id} for regular hours the student actually offered
    this month (never assign an hour a student didn't select).

    Keyed by lane as well as date and hour: an hour staffed in both lanes has
    two slots, and without the lane one of them would silently shadow the
    other, so half the standing arrangements would never be locked in."""
    slot_id_by_cell = {(s.date, s.hour, s.track): s.id for s in slots}
    locked = {}
    for r in RegularSlot.query.filter_by(month_id=month_id, state="assigned").all():
        slot_id = slot_id_by_cell.get((r.date, r.hour, r.track))
        if r.student_id and slot_id and slot_id in offered.get(r.student_id, ()):
            locked[slot_id] = r.student_id
    return locked


def _load_tracks(student_ids):
    """{student_id: track} for the students in play."""
    from app.models import Student
    from app.utils.tracks import track_for, DEFAULT_TRACK
    if not student_ids:
        return {}
    rows = Student.query.filter(Student.id.in_(list(student_ids))).all()
    out = {s.id: track_for(s) for s in rows}
    return {sid: out.get(sid, DEFAULT_TRACK) for sid in student_ids}


def _half_days(slots):
    """{(date, period, track): [slots in hour order]} — the windows a session
    lives in.

    The lane is part of the key and has to be. Two lanes put two slots on the
    same hour, and the run-building below tests contiguity with
    `s.hour == cur[-1].hour + 1` — so a bucket holding both lanes' 08:00 fails
    that test at every step and shreds every run into single hours. A four
    hour morning would come out as four one-hour sessions for four different
    students, which is exactly the allocation CLAUDE.md #7 says not to go back
    to."""
    out = defaultdict(list)
    for s in slots:
        out[(s.date, s.period, s.track)].append(s)
    for v in out.values():
        v.sort(key=lambda s: s.hour)
    return out


def _candidate_sessions(slots, offered, rules):
    """Every session a student could be given: (student_id, half_day_key,
    [slot_ids]) for each unbroken run of hours they offered within a
    half-day, from 1 (if short sessions are allowed) up to max_hours."""
    shortest = 1 if rules["short_as_last_resort"] else rules["min_hours"]
    longest = rules["max_hours"]
    out = []
    for key, day_slots in _half_days(slots).items():
        for student_id, mine in offered.items():
            # Split the half-day into the student's maximal offered runs of
            # consecutive hours (a missing slot or an unoffered hour breaks it).
            runs, cur = [], []
            for s in day_slots:
                if s.id in mine and cur and s.hour == cur[-1].hour + 1:
                    cur.append(s)
                else:
                    if cur:
                        runs.append(cur)
                    cur = [s] if s.id in mine else []
            if cur:
                runs.append(cur)
            for run in runs:
                for length in range(shortest, min(longest, len(run)) + 1):
                    for start in range(len(run) - length + 1):
                        out.append((student_id, key, [s.id for s in run[start:start + length]]))
    return out


def solve_month(month_id, weights, floor_hours, rules=None, time_limit_seconds=20):
    """Returns ({slot_id: student_id}, meta) for the best schedule found."""
    if rules is None:
        from app.utils.settings import get_session_rules
        rules = get_session_rules()

    slots, offered = _load_inputs(month_id)
    locked = _load_regular_locks(month_id, slots, offered)
    base_meta = {"total_slots": len(slots), "session_rules": rules}

    if not offered:
        return {}, dict(base_meta, status="NO_AVAILABILITY", assigned=0, regular_locked=0,
                        regular_locked_slot_ids=[])

    slot_by_id = {s.id: s for s in slots}
    students = sorted(offered)
    track_by_student = _load_tracks(students)
    candidates = _candidate_sessions(slots, offered, rules)

    model = cp_model.CpModel()
    z = [model.NewBoolVar(f"sess_{i}") for i in range(len(candidates))]

    # x[(student, slot)]: the student works that hour — 1 exactly when one of
    # their chosen sessions covers it.
    covering = defaultdict(list)            # (student, slot) -> [z]
    per_half_day = defaultdict(list)        # (student, date, period) -> [z]
    for var, (student_id, (d, period, _track), slot_ids) in zip(z, candidates):
        # Keyed without the lane on purpose: a student works one lane, so this
        # is already one session per student per half-day, and leaving the lane
        # out keeps it that way even if a student ever straddled both.
        per_half_day[(student_id, d, period)].append(var)
        for slot_id in slot_ids:
            covering[(student_id, slot_id)].append(var)

    # Hard: at most one session per student per half-day. That also makes a
    # student's sessions disjoint, so each x below is 0 or 1.
    for vars_ in per_half_day.values():
        model.AddAtMostOne(vars_)

    x = {}
    for (student_id, slot_id), vars_ in covering.items():
        v = model.NewBoolVar(f"x_s{student_id}_sl{slot_id}")
        model.Add(v == sum(vars_))
        x[(student_id, slot_id)] = v

    # Hard: at most one student per slot.
    by_slot = defaultdict(list)
    for (student_id, slot_id), v in x.items():
        by_slot[slot_id].append(v)
    for vars_ in by_slot.values():
        model.AddAtMostOne(vars_)

    terms = []

    # Regular hours — see REGULAR_LOCK_FACTOR.
    lock_vars = [x[(sid, slot_id)] for slot_id, sid in locked.items() if (sid, slot_id) in x]
    if lock_vars:
        terms.append(REGULAR_LOCK_FACTOR * weights["coverage"] * sum(lock_vars))

    # 1. Coverage
    terms.append(weights["coverage"] * sum(x.values()))

    # Hours per student (floor + equalise).
    hours = {}
    for student_id in students:
        mine = [v for (sid, _), v in x.items() if sid == student_id]
        h = model.NewIntVar(0, len(offered[student_id]), f"hours_s{student_id}")
        model.Add(h == sum(mine))
        hours[student_id] = h

    # 2. Floor guarantee: shortfall against min(floor, hours offered).
    shortfalls = []
    for student_id in students:
        target = min(floor_hours, len(offered[student_id]))
        if target > 0:
            short = model.NewIntVar(0, target, f"floor_short_s{student_id}")
            model.Add(short >= target - hours[student_id])
            shortfalls.append(short)
    if shortfalls:
        terms.append(-weights["floor_guarantee"] * sum(shortfalls))

    # 3 + 4. Session costs: every session costs per_session; one shorter than
    # the minimum also costs short_session per missing hour.
    short_cost = []
    for var, (_, _, slot_ids) in zip(z, candidates):
        missing = rules["min_hours"] - len(slot_ids)
        if missing > 0:
            short_cost.append(missing * var)
    if short_cost:
        terms.append(-weights["short_session"] * sum(short_cost))
    terms.append(-weights["per_session"] * sum(z))

    # 5. Same student, both halves of one day.
    halves = defaultdict(dict)  # (student, date) -> {period: [z]}
    for (student_id, d, period), vars_ in per_half_day.items():
        halves[(student_id, d)][period] = vars_
    doubles = []
    for (student_id, d), by_period in halves.items():
        if len(by_period) < 2:
            continue
        am = sum(by_period.get("morning", []))
        pm = sum(by_period.get("afternoon", []))
        if rules["allow_same_day_double"]:
            both = model.NewBoolVar(f"double_s{student_id}_{d}")
            model.Add(both >= am + pm - 1)
            doubles.append(both)
        else:
            model.Add(am + pm <= 1)
    if doubles:
        terms.append(-weights["same_day_double"] * sum(doubles))

    # 6. Low churn: the same weekday/hour switching on/off across consecutive
    # ISO weeks. What lets a 2-on/2-off rotation emerge — never scripted.
    # Lane included: without it, the two lanes' slots for one weekday/hour
    # share a bucket and each week keeps only whichever was seen last, so
    # half the consistency signal would quietly vanish.
    weekday_hour_week = defaultdict(dict)  # (weekday, hour, track) -> {iso week: slot_id}
    for s in slots:
        weekday_hour_week[(s.date.weekday(), s.hour, s.track)][s.date.isocalendar()[1]] = s.id
    churn = []
    for student_id in students:
        for week_map in weekday_hour_week.values():
            weeks = sorted(week_map)
            for w1, w2 in zip(weeks, weeks[1:]):
                a, b = x.get((student_id, week_map[w1])), x.get((student_id, week_map[w2]))
                if w2 - w1 != 1 or a is None or b is None:
                    continue
                c = model.NewBoolVar(f"churn_s{student_id}_{w1}_{week_map[w1]}")
                model.Add(c >= a - b)
                model.Add(c >= b - a)
                churn.append(c)
    if churn:
        terms.append(-weights["low_churn"] * sum(churn))

    # 7. Equalise hours among students who offered anything — within a lane,
    #    not across them. Paid and unpaid workers offer very different volumes
    #    and are staffed for different reasons, so one shared spread would pull
    #    the paid allocations toward the unpaid ones and quietly undercut the
    #    students who rely on the pay.
    students_by_track = defaultdict(list)
    for student_id in students:
        students_by_track[track_by_student[student_id]].append(student_id)
    for track, ids in sorted(students_by_track.items()):
        if len(ids) < 2:
            continue
        top = max(len(offered[sid]) for sid in ids)
        hi = model.NewIntVar(0, top, f"max_hours_{track}")
        lo = model.NewIntVar(0, top, f"min_hours_{track}")
        for sid in ids:
            model.Add(hours[sid] <= hi)
            model.Add(hours[sid] >= lo)
        terms.append(-weights["equalise_hours"] * (hi - lo))

    model.Maximize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)
    meta = dict(base_meta, status=solver.StatusName(status),
                wall_time_seconds=round(solver.WallTime(), 2), candidate_sessions=len(candidates))

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result = {slot_id: sid for (sid, slot_id), v in x.items() if solver.Value(v)}
        chosen = [c for var, c in zip(z, candidates) if solver.Value(var)]
        meta["objective_value"] = solver.ObjectiveValue()
    else:
        result, chosen = _greedy_sessions(slots, offered, locked, rules)
        meta["fallback"] = "greedy_sessions"

    honoured = [slot_id for slot_id, sid in locked.items() if result.get(slot_id) == sid]
    lengths = [len(c[2]) for c in chosen]
    meta.update({
        "assigned": len(result),
        "sessions": len(chosen),
        "session_lengths": {str(n): lengths.count(n) for n in sorted(set(lengths))},
        "regular_locked": len(honoured),
        "regular_locked_slot_ids": honoured,
        "regular_not_honoured": len(locked) - len(honoured),
    })
    return result, meta


def _greedy_sessions(slots, offered, locked, rules):
    """Fallback, fully explainable: seed regular hours, then go half-day by
    half-day and keep handing a still-open run to whoever has the fewest
    hours so far (full-length runs before short ones), until nobody can take
    one."""
    shortest = 1 if rules["short_as_last_resort"] else rules["min_hours"]
    longest = rules["max_hours"]
    hours = defaultdict(int)
    result, chosen = {}, []

    for slot_id, sid in locked.items():
        result[slot_id] = sid
        hours[sid] += 1

    half_days = _half_days(slots)
    for key in sorted(half_days, key=lambda k: (k[0], k[1] != "morning")):
        day_slots = half_days[key]
        seated = {result[s.id] for s in day_slots if s.id in result}
        while True:
            best = None
            for sid in sorted(offered):
                if sid in seated:
                    continue
                run, longest_run = [], []
                for s in day_slots:
                    free = s.id not in result and s.id in offered[sid]
                    if free and run and s.hour == run[-1].hour + 1:
                        run.append(s)
                    else:
                        run = [s] if free else []
                    if len(run) > len(longest_run):
                        longest_run = list(run)
                longest_run = longest_run[:longest]
                if len(longest_run) < shortest:
                    continue
                # Full-length sessions first; a short one only if that's all
                # that's left. Then fewest hours so far, then the longer run.
                rank = (len(longest_run) < rules["min_hours"], hours[sid], -len(longest_run), sid)
                if best is None or rank < best[0]:
                    best = (rank, sid, longest_run)
            if best is None:
                break
            _, sid, run = best
            for s in run:
                result[s.id] = sid
            hours[sid] += len(run)
            seated.add(sid)
            chosen.append((sid, key, [s.id for s in run]))
    return result, chosen
