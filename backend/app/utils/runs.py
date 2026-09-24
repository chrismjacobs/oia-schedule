"""Contiguous runs of hourly slots.

The hour is the unit the data is stored in, but it is almost never the unit
a human should be told about: a student signs in once for a whole run
(CLAUDE.md #9), the solver hands out sessions rather than hours
(CLAUDE.md #7), and a notification about a four-hour morning is one piece of
news, not four. Everything that turns slots into a message goes through here
so they all draw the same boundaries.
"""


def contiguous_runs(items, hour_of):
    """Split hour-keyed items into runs of consecutive hours.

    Callers must pass items that already share a date (and a student, where
    that matters) — this only looks at the hour.

    Plain hour+1 adjacency is deliberately all it takes: SLOT_HOURS skips
    12:00, so a morning's 11:00 and an afternoon's 13:00 fall apart on their
    own and no run can straddle lunch.
    """
    runs = []
    for item in sorted(items, key=hour_of):
        if runs and hour_of(item) == hour_of(runs[-1][-1]) + 1:
            runs[-1].append(item)
        else:
            runs.append([item])
    return runs


def span_text(slots):
    """How a run reads in a message: '2026-09-17 8:00-12:00'.

    The end is the last hour + 1 because a slot is the hour it starts — the
    11:00 slot is worked until 12:00, and telling students a shift ends at
    11:00 when they are expected until noon is the kind of small wrongness
    that gets an overseer a phone call.
    """
    ordered = sorted(slots, key=lambda s: s.hour)
    first, last = ordered[0], ordered[-1]
    return f"{first.date.isoformat()} {first.hour}:00-{last.hour + 1}:00"
