"""period_key derivation for regular_task cadence guarding (SCHEMA.md
task_completion notes): the unique index on (regular_task_id, period_key)
is what makes "only once per period" trivial."""
import calendar
import uuid
from datetime import date


def weekdays_in_month(year_month):
    """Every Mon-Fri date in a 'YYYY-MM' month, closed dates or not — the
    raw calendar shape. Callers that care about closed dates filter after."""
    year, month = (int(x) for x in year_month.split("-"))
    _, days_in_month = calendar.monthrange(year, month)
    out = []
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        if d.weekday() < 5:
            out.append(d)
    return out


def period_key_for(frequency, on_date):
    if frequency == "daily":
        return on_date.isoformat()
    if frequency == "weekly":
        iso_year, iso_week, _ = on_date.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if frequency == "monthly":
        return f"{on_date.year:04d}-{on_date.month:02d}"
    if frequency == "unlimited":
        # A regular duty, not a cadence (CLAUDE.md #10) — every completion
        # needs its own never-repeating key so the (regular_task_id,
        # period_key) uniqueness constraint never blocks a repeat and the
        # task never drops off the available-tasks list.
        return f"u-{uuid.uuid4()}"
    raise ValueError(f"unknown frequency: {frequency}")
