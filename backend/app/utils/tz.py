"""Every timestamp in this app is a naive datetime interpreted as Asia/Taipei
wall-clock time (SCHEMA.md: "Timestamps are ISO 8601 with timezone
(Asia/Taipei)"). That's what admins type into <input type="datetime-local">
pickers and what students see, so it's what gets stored and compared against
— never UTC. Use local_now() everywhere `datetime.utcnow()` might otherwise
be reached for; mixing the two is exactly what breaks selection-window /
sign-in-window / no-show comparisons on any server not physically in Taipei.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")


def local_now():
    return datetime.now(TAIPEI).replace(tzinfo=None)


def local_today():
    """date.today() relies on the OS timezone, which is UTC on most hosting
    (Render's containers included) — use this instead wherever "today" means
    the Taipei calendar date, not the server's."""
    return local_now().date()
