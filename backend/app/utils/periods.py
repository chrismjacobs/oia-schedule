"""period_key derivation for regular_task cadence guarding (SCHEMA.md
task_completion notes): the unique index on (regular_task_id, period_key)
is what makes "only once per period" trivial."""


def period_key_for(frequency, on_date):
    if frequency == "daily":
        return on_date.isoformat()
    if frequency == "weekly":
        iso_year, iso_week, _ = on_date.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if frequency == "monthly":
        return f"{on_date.year:04d}-{on_date.month:02d}"
    raise ValueError(f"unknown frequency: {frequency}")
