"""OR-Tools CP-SAT allocator (CLAUDE.md #6). Deterministic, auditable,
config-driven. No LLM involved in allocation, ever.

Hard constraints: at most one student per slot; never assign an hour a
student didn't offer. (No-double-booking and closed-date exclusion fall out
for free: slots are unique per date+hour, and closed dates never get slots.)

Soft preferences, in priority order (encoded as weighted objective terms so
higher-priority terms use larger weights and dominate lower ones):
  1. coverage        - maximise filled slots
  2. floor_guarantee  - everyone who offered enough hours gets >= floor before
                        anyone gets extra
  3. contiguity       - reward adjacent same-student hours (~2h blocks)
  4. low_churn        - reward repeating the same weekday/hour across
                        consecutive weeks (this is what makes a 2-on/2-off
                        rotation emerge on its own; never scripted directly)
  5. equalise_hours   - minimise the spread between the most- and
                        least-scheduled student

If CP-SAT can't produce a feasible solution in the time budget, fall back to
a greedy round-robin (CLAUDE.md #6: "acceptable and fully explainable").
"""
from collections import defaultdict

from ortools.sat.python import cp_model

from app.models import Slot, Availability


def _load_inputs(month_id):
    slots = Slot.query.filter_by(month_id=month_id).order_by(Slot.date, Slot.hour).all()
    avail_rows = (
        Availability.query.join(Slot, Availability.slot_id == Slot.id)
        .filter(Slot.month_id == month_id)
        .all()
    )
    by_slot = defaultdict(list)  # slot_id -> [student_id]
    by_student = defaultdict(list)  # student_id -> [slot_id]
    for a in avail_rows:
        by_slot[a.slot_id].append(a.student_id)
        by_student[a.student_id].append(a.slot_id)
    return slots, by_slot, by_student


def solve_month(month_id, weights, floor_hours, time_limit_seconds=20):
    """Returns {slot_id: student_id} for the best assignment found, plus a
    dict of solver metadata for the audit trail."""
    slots, by_slot, by_student = _load_inputs(month_id)
    if not by_student:
        return {}, {"status": "NO_AVAILABILITY", "assigned": 0, "total_slots": len(slots)}

    slot_by_id = {s.id: s for s in slots}
    students = list(by_student.keys())

    model = cp_model.CpModel()

    # x[(student, slot)] only exists where the student actually offered the slot.
    x = {}
    for student_id, slot_ids in by_student.items():
        for slot_id in slot_ids:
            x[(student_id, slot_id)] = model.NewBoolVar(f"x_s{student_id}_sl{slot_id}")

    # Hard: at most one student per slot.
    for slot_id, student_ids in by_slot.items():
        model.Add(sum(x[(sid, slot_id)] for sid in student_ids) <= 1)

    objective_terms = []

    # 1. Coverage
    coverage_terms = list(x.values())
    if coverage_terms:
        objective_terms.append(weights["coverage"] * sum(coverage_terms))

    # Per-student assigned-hours variable (used by floor + equalise).
    max_possible = max((len(v) for v in by_student.values()), default=0)
    assigned_hours = {}
    for student_id in students:
        var = model.NewIntVar(0, max_possible, f"hours_s{student_id}")
        model.Add(var == sum(x[(student_id, sid)] for sid in by_student[student_id]))
        assigned_hours[student_id] = var

    # 2. Floor guarantee: shortfall against min(floor_hours, offered hours).
    shortfall_terms = []
    for student_id in students:
        target = min(floor_hours, len(by_student[student_id]))
        if target <= 0:
            continue
        shortfall = model.NewIntVar(0, target, f"shortfall_s{student_id}")
        model.Add(shortfall >= target - assigned_hours[student_id])
        shortfall_terms.append(shortfall)
    if shortfall_terms:
        objective_terms.append(-weights["floor_guarantee"] * sum(shortfall_terms))

    # 3. Contiguity: reward same-student adjacent-hour pairs.
    contiguity_terms = []
    slots_by_date = defaultdict(dict)  # date -> {hour: slot_id}
    for s in slots:
        slots_by_date[s.date][s.hour] = s.id
    for student_id, slot_ids in by_student.items():
        student_slot_set = set(slot_ids)
        for date, hour_map in slots_by_date.items():
            for hour, slot_id in hour_map.items():
                next_slot_id = hour_map.get(hour + 1)
                if next_slot_id is None:
                    continue
                if slot_id in student_slot_set and next_slot_id in student_slot_set:
                    y = model.NewBoolVar(f"contig_s{student_id}_{slot_id}_{next_slot_id}")
                    model.Add(y <= x[(student_id, slot_id)])
                    model.Add(y <= x[(student_id, next_slot_id)])
                    contiguity_terms.append(y)
    if contiguity_terms:
        objective_terms.append(weights["contiguity"] * sum(contiguity_terms))

    # 4. Low churn: penalise switching on/off at the same weekday/hour across
    # consecutive ISO weeks. This is what lets a 2-weeks-on/2-weeks-off
    # rotation emerge on its own — never scripted.
    weekday_hour_week = defaultdict(dict)  # (weekday, hour) -> {week_num: slot_id}
    for s in slots:
        weekday_hour_week[(s.date.weekday(), s.hour)][s.date.isocalendar()[1]] = s.id

    churn_terms = []
    for student_id, slot_ids in by_student.items():
        student_slot_set = set(slot_ids)
        for (weekday, hour), week_map in weekday_hour_week.items():
            weeks = sorted(week_map.keys())
            for w1, w2 in zip(weeks, weeks[1:]):
                if w2 - w1 != 1:
                    continue
                slot1, slot2 = week_map[w1], week_map[w2]
                if slot1 not in student_slot_set or slot2 not in student_slot_set:
                    continue
                churn = model.NewIntVar(0, 1, f"churn_s{student_id}_{slot1}_{slot2}")
                model.Add(churn >= x[(student_id, slot1)] - x[(student_id, slot2)])
                model.Add(churn >= x[(student_id, slot2)] - x[(student_id, slot1)])
                churn_terms.append(churn)
    if churn_terms:
        objective_terms.append(-weights["low_churn"] * sum(churn_terms))

    # 5. Equalise hours: minimise spread among students who offered anything.
    if len(students) > 1 and max_possible > 0:
        max_h = model.NewIntVar(0, max_possible, "max_hours")
        min_h = model.NewIntVar(0, max_possible, "min_hours")
        for student_id in students:
            model.Add(assigned_hours[student_id] <= max_h)
            model.Add(assigned_hours[student_id] >= min_h)
        spread = model.NewIntVar(0, max_possible, "spread")
        model.Add(spread == max_h - min_h)
        objective_terms.append(-weights["equalise_hours"] * spread)

    model.Maximize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 8
    status = solver.Solve(model)

    meta = {
        "status": solver.StatusName(status),
        "total_slots": len(slots),
        "wall_time_seconds": round(solver.WallTime(), 2),
    }

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result = {}
        for (student_id, slot_id), var in x.items():
            if solver.Value(var) == 1:
                result[slot_id] = student_id
        meta["assigned"] = len(result)
        meta["objective_value"] = solver.ObjectiveValue()
        return result, meta

    # Fallback: greedy round-robin (CLAUDE.md #6 — explainable, acceptable if
    # CP-SAT can't produce a feasible solution in the time budget).
    result = _greedy_round_robin(slots, by_slot, floor_hours)
    meta["assigned"] = len(result)
    meta["fallback"] = "greedy_round_robin"
    return result, meta


def _greedy_round_robin(slots, by_slot, floor_hours):
    """Repeatedly give the next contested hour to the eligible student with
    the fewest hours so far. Deterministic, fully explainable."""
    hours_so_far = defaultdict(int)
    result = {}
    for slot in slots:
        candidates = by_slot.get(slot.id, [])
        if not candidates:
            continue
        best = min(candidates, key=lambda sid: (hours_so_far[sid], sid))
        result[slot.id] = best
        hours_so_far[best] += 1
    return result
