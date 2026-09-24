"""Worker lanes (tracks).

Some hours are staffed by two people at once: one paid Official Worker and
one unpaid Service Worker. They never collide — there is never a second OW or
a second SW in the same hour — so rather than giving a slot a capacity and
letting two assignments share it, each (date, hour, track) is its own slot row
with its own single occupant. The lanes are merged only when a grid is drawn.

That keeps every "one student per slot" assumption in the app true: the
assignment uniqueness constraint stands, slot.state stays binary, and an
unfilled SW hour is simply an uncovered slot rather than a half-covered one
somebody has to remember to count.
"""

# Keys stored in slot.track / regular_slot.track / regular_slot_template.track.
TRACKS = {
    "OW": "Official Worker (paid)",
    "SW": "Service Worker / TA (unpaid)",
}

DEFAULT_TRACK = "OW"

# Whether an hour with no regular_slot row at all gets a slot in this lane.
#
# The OW lane keeps the original rule: every weekday hour is staffed unless
# the regular grid says otherwise. The SW lane has to work the other way round
# — applying "no row means staff it" to a second lane would double every hour
# in the month. An SW slot exists only where the overseer has said it should.
TRACK_DEFAULT_ON = {"OW": True, "SW": False}


def track_for(student):
    """Which lane a student works in.

    TA sits in the unpaid lane alongside SW — kept in one function so that
    promoting TA to a lane of its own later is a one-line change here, not a
    migration.

    worker_type is nullable and existing students start as "not set"; those
    default to the paid lane, and the dashboard flags anyone unclassified so
    the default can't quietly become the answer.
    """
    if student is None:
        return DEFAULT_TRACK
    return "SW" if student.worker_type in ("SW", "TA") else "OW"
