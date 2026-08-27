"""Colour x shape student identity tokens (CLAUDE.md #14).

Exhaust the 8-colour palette first (colour is the fastest channel); only once
every colour is in use for the semester does a new student reuse a colour,
differentiated by shape.
"""
from app.models import STUDENT_PALETTE, STUDENT_SHAPES, Student


def assign_token(semester_id):
    """Return (colour, shape) for a new student in this semester, exhausting
    colours before reusing one with a different shape."""
    existing = Student.query.filter_by(semester_id=semester_id).all()
    used_colours = {s.colour for s in existing}
    used_pairs = {(s.colour, s.shape) for s in existing}

    for colour in STUDENT_PALETTE:
        if colour not in used_colours:
            return colour, STUDENT_SHAPES[0]

    for shape in STUDENT_SHAPES:
        for colour in STUDENT_PALETTE:
            if (colour, shape) not in used_pairs:
                return colour, shape

    raise ValueError("Student identity token space exhausted (32 max per semester)")
