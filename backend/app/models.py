import re
import uuid

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db
from app.utils.tz import local_now

# --- Managed palette (CLAUDE.md #14) — exhaust colours before reusing with a shape ---
STUDENT_PALETTE = [
    "#0072B2",  # blue
    "#E69F00",  # orange
    "#009E73",  # green
    "#D55E00",  # vermilion
    "#7E57C2",  # purple
    "#0EA5A5",  # teal
    "#C2185B",  # magenta
    "#8D6E63",  # brown
]
STUDENT_SHAPES = ["circle", "triangle", "square", "diamond"]

# Letters and digits, any length. The original brief specified 8 numeric
# digits, but the real roster turned out to carry IDs that don't fit that
# shape, so the rule is now just "alphanumeric, non-empty" — no length or
# prefix assumption. Lives here rather than in one blueprint because two
# places enforce it: registration (auth/routes.py) and the overseer's inline
# edit (admin/routes.py).
STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9]+$")
STUDENT_ID_MAX = 32

SLOT_HOURS = [8, 9, 10, 11, 13, 14, 15, 16]  # 1-hour slots, Mon-Fri (CLAUDE.md #4)

REGULAR_SLOT_STATES = ["unavailable", "unassigned", "assigned"]

REGULAR_TASK_FREQUENCIES = ["daily", "weekly", "monthly", "unlimited"]

MONTH_STATES = [
    "setup", "selection_open", "selection_closed", "draft",
    "review", "committed", "running", "closed",
]


def gen_uuid():
    return str(uuid.uuid4())


class Semester(db.Model):
    __tablename__ = "semester"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    starts_on = db.Column(db.Date, nullable=False)
    ends_on = db.Column(db.Date, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    students = db.relationship("Student", back_populates="semester")


class Student(db.Model):
    __tablename__ = "student"
    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False)
    chinese_name = db.Column(db.String(64), nullable=False)
    english_name = db.Column(db.String(64), nullable=False)
    student_id = db.Column(db.String(STUDENT_ID_MAX), nullable=False, unique=True)
    colour = db.Column(db.String(7), nullable=False)
    shape = db.Column(db.String(16), nullable=False)
    # Insurance (勞保) number. Admin-managed only: not captured at registration
    # and deliberately absent from to_dict() — that payload goes to students in
    # the team schedule and roster views, and this is nobody's business but the
    # overseer's. Served only by the overseer-gated /api/admin/students.
    insurance_number = db.Column(db.String(32), nullable=True)
    line_user_id = db.Column(db.String(64), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)  # seeded row — "Reset demo data" deletes these
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    semester = db.relationship("Semester", back_populates="students")
    user = db.relationship("User", back_populates="student", uselist=False)

    __table_args__ = (
        # student_id's shape is validated in app code (STUDENT_ID_RE above), not a
        # DB check constraint — SQLite (the v1 target) has no portable regex
        # constraint syntax. Uniqueness *is* enforced by the column, though only
        # exactly; the case-insensitive check lives in app code too, so "A12" and
        # "a12" can't both be created.
        db.UniqueConstraint("semester_id", "colour", "shape", name="uq_student_token_per_semester"),
    )

    @property
    def short_name(self):
        return self.english_name or self.chinese_name

    def display_name(self):
        if self.chinese_name and self.english_name:
            return f"{self.chinese_name} {self.english_name}"
        return self.chinese_name or self.english_name

    def to_dict(self):
        return {
            "id": self.id,
            "semester_id": self.semester_id,
            "chinese_name": self.chinese_name,
            "english_name": self.english_name,
            "student_id": self.student_id,
            "colour": self.colour,
            "shape": self.shape,
            "is_active": self.is_active,
            "is_demo": self.is_demo,
        }


class User(UserMixin, db.Model):
    __tablename__ = "app_user"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True, unique=True)
    email = db.Column(db.String(128), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False)  # overseer | student
    invite_token = db.Column(db.String(36), nullable=True, unique=True)
    invite_accepted_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    student = db.relationship("Student", back_populates="user")

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "student": self.student.to_dict() if self.student else None,
        }


class Month(db.Model):
    __tablename__ = "month"
    id = db.Column(db.Integer, primary_key=True)
    year_month = db.Column(db.String(7), nullable=False, unique=True)  # "2026-09"
    state = db.Column(db.String(20), nullable=False, default="setup")

    closed_dates = db.relationship("ClosedDate", back_populates="month")
    selection_window = db.relationship("SelectionWindow", back_populates="month", uselist=False)
    slots = db.relationship("Slot", back_populates="month")
    schedules = db.relationship("Schedule", back_populates="month")

    def to_dict(self):
        return {"id": self.id, "year_month": self.year_month, "state": self.state}


class ClosedDate(db.Model):
    __tablename__ = "closed_date"
    id = db.Column(db.Integer, primary_key=True)
    month_id = db.Column(db.Integer, db.ForeignKey("month.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, unique=True)
    reason = db.Column(db.String(255), nullable=True)
    set_by = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)

    month = db.relationship("Month", back_populates="closed_dates")

    def to_dict(self):
        return {"id": self.id, "date": self.date.isoformat(), "reason": self.reason}


class SelectionWindow(db.Model):
    __tablename__ = "selection_window"
    id = db.Column(db.Integer, primary_key=True)
    month_id = db.Column(db.Integer, db.ForeignKey("month.id"), nullable=False, unique=True)
    opens_at = db.Column(db.DateTime, nullable=False)
    closes_at = db.Column(db.DateTime, nullable=False)

    month = db.relationship("Month", back_populates="selection_window")

    def to_dict(self):
        return {
            "id": self.id,
            "month_id": self.month_id,
            "opens_at": self.opens_at.isoformat(),
            "closes_at": self.closes_at.isoformat(),
        }


class Slot(db.Model):
    __tablename__ = "slot"
    id = db.Column(db.Integer, primary_key=True)
    month_id = db.Column(db.Integer, db.ForeignKey("month.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    hour = db.Column(db.Integer, nullable=False)
    period = db.Column(db.String(16), nullable=False)  # morning | afternoon
    state = db.Column(db.String(16), nullable=False, default="open")  # open|assigned|reopened

    month = db.relationship("Month", back_populates="slots")
    availabilities = db.relationship("Availability", back_populates="slot")
    assignments = db.relationship("Assignment", back_populates="slot")

    __table_args__ = (db.UniqueConstraint("date", "hour", name="uq_slot_date_hour"),)

    def to_dict(self):
        return {
            "id": self.id,
            "date": self.date.isoformat(),
            "hour": self.hour,
            "period": self.period,
            "state": self.state,
        }


class RegularSlotTemplate(db.Model):
    """Persistent weekly pattern for standing/regular assignments — some
    students work the same hour every week. Not month-specific: copied into
    `regular_slot` rows when a month's regular schedule is populated. Editing
    this only affects months populated afterward."""
    __tablename__ = "regular_slot_template"
    id = db.Column(db.Integer, primary_key=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Mon .. 4=Fri
    hour = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(16), nullable=False, default="unassigned")  # unavailable|unassigned|assigned
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True)  # only when state=assigned

    student = db.relationship("Student")

    __table_args__ = (db.UniqueConstraint("weekday", "hour", name="uq_regular_template_weekday_hour"),)

    def to_dict(self):
        return {
            "id": self.id,
            "weekday": self.weekday,
            "hour": self.hour,
            "state": self.state,
            "student_id": self.student_id,
        }


class RegularSlot(db.Model):
    """One month's instance of the regular-slot pattern, per (date, hour) —
    populated from `regular_slot_template` for that month, then hand-edited by
    the overseer for one-off exceptions without touching the master template.
    `state=unavailable` means no `Slot` is generated for that hour at all
    (coverage need varies month to month — not every hour needs staffing).
    `state=assigned` is a standing claim the solver locks in first, but only
    if that student actually offers the hour that month; otherwise it's open
    to whoever did offer it, same as any other slot."""
    __tablename__ = "regular_slot"
    id = db.Column(db.Integer, primary_key=True)
    month_id = db.Column(db.Integer, db.ForeignKey("month.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    hour = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(16), nullable=False, default="unassigned")  # unavailable|unassigned|assigned
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True)  # only when state=assigned

    month = db.relationship("Month")
    student = db.relationship("Student")

    __table_args__ = (db.UniqueConstraint("date", "hour", name="uq_regular_slot_date_hour"),)

    def to_dict(self):
        return {
            "id": self.id,
            "month_id": self.month_id,
            "date": self.date.isoformat(),
            "hour": self.hour,
            "state": self.state,
            "student_id": self.student_id,
        }


class Availability(db.Model):
    __tablename__ = "availability"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=False)
    submitted_at = db.Column(db.DateTime, nullable=False, default=local_now)

    student = db.relationship("Student")
    slot = db.relationship("Slot", back_populates="availabilities")

    __table_args__ = (db.UniqueConstraint("student_id", "slot_id", name="uq_availability_student_slot"),)


class Schedule(db.Model):
    __tablename__ = "schedule"
    id = db.Column(db.Integer, primary_key=True)
    month_id = db.Column(db.Integer, db.ForeignKey("month.id"), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="draft")  # draft|committed
    generated_at = db.Column(db.DateTime, nullable=False, default=local_now)
    committed_at = db.Column(db.DateTime, nullable=True)
    solver_weights = db.Column(db.JSON, nullable=True)
    is_demo = db.Column(db.Boolean, nullable=False, default=False)  # seeded schedule — cleared by "Reset demo data"

    month = db.relationship("Month", back_populates="schedules")
    assignments = db.relationship("Assignment", back_populates="schedule", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "month_id": self.month_id,
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "committed_at": self.committed_at.isoformat() if self.committed_at else None,
            "solver_weights": self.solver_weights,
            "is_demo": self.is_demo,
        }


class Assignment(db.Model):
    __tablename__ = "assignment"
    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedule.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    source = db.Column(db.String(16), nullable=False)  # solver|manual_edit|claimed|regular_lock
    created_at = db.Column(db.DateTime, nullable=False, default=local_now)

    schedule = db.relationship("Schedule", back_populates="assignments")
    slot = db.relationship("Slot", back_populates="assignments")
    student = db.relationship("Student")

    __table_args__ = (db.UniqueConstraint("schedule_id", "slot_id", name="uq_assignment_schedule_slot"),)

    def to_dict(self):
        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "slot_id": self.slot_id,
            "student_id": self.student_id,
            "source": self.source,
        }


class LeaveRequest(db.Model):
    __tablename__ = "leave_request"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=False)
    reason = db.Column(db.Text, nullable=False)
    requested_at = db.Column(db.DateTime, nullable=False, default=local_now)
    lead_time_hours = db.Column(db.Float, nullable=True)
    status = db.Column(db.String(16), nullable=False, default="pending")  # pending|approved|denied
    decided_by = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)

    student = db.relationship("Student")
    slot = db.relationship("Slot")

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "slot_id": self.slot_id,
            "reason": self.reason,
            "requested_at": self.requested_at.isoformat(),
            "lead_time_hours": self.lead_time_hours,
            "status": self.status,
        }


class ReopenedSlot(db.Model):
    """A slot open for FCFS claim. Three ways one of these gets created:
    an approved leave request (leave_request_id set), the /tick job noticing
    a never-filled committed slot as its date approaches (source=auto_unfilled),
    or the overseer manually advertising an uncovered slot — e.g. leave taken
    off the books, without a LeaveRequest (source=manual).

    Retracting one (overseer pulls the offer back before anyone claims it)
    stamps `retracted_at` rather than deleting the row: /tick's auto-advertise
    skips any slot that already has a reopened_slot row, so the tombstone is
    what stops it from immediately re-advertising what was just withdrawn."""
    __tablename__ = "reopened_slot"
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=False)
    leave_request_id = db.Column(db.Integer, db.ForeignKey("leave_request.id"), nullable=True)
    source = db.Column(db.String(16), nullable=False, default="leave")  # leave|auto_unfilled|manual
    opened_at = db.Column(db.DateTime, nullable=False, default=local_now)
    claimed_by = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    retracted_at = db.Column(db.DateTime, nullable=True)  # withdrawn by the overseer, never claimed

    slot = db.relationship("Slot")
    leave_request = db.relationship("LeaveRequest")
    claimer = db.relationship("Student")

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "leave_request_id": self.leave_request_id,
            "source": self.source,
            "opened_at": self.opened_at.isoformat(),
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "retracted_at": self.retracted_at.isoformat() if self.retracted_at else None,
        }


class AttendanceSession(db.Model):
    __tablename__ = "attendance_session"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    signed_in_at = db.Column(db.DateTime, nullable=False)
    signed_out_at = db.Column(db.DateTime, nullable=True)
    flagged = db.Column(db.Boolean, nullable=False, default=False)
    flag_reason = db.Column(db.String(64), nullable=True)  # forgot_sign_out | not_scheduled

    student = db.relationship("Student")
    hourly_reports = db.relationship("HourlyReport", back_populates="session", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "date": self.date.isoformat(),
            "signed_in_at": self.signed_in_at.isoformat(),
            "signed_out_at": self.signed_out_at.isoformat() if self.signed_out_at else None,
            "flagged": self.flagged,
            "flag_reason": self.flag_reason,
        }


class HourlyReport(db.Model):
    __tablename__ = "hourly_report"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("attendance_session.id"), nullable=False)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=False)
    note = db.Column(db.Text, nullable=True)

    session = db.relationship("AttendanceSession", back_populates="hourly_reports")
    slot = db.relationship("Slot")
    task_completions = db.relationship("TaskCompletion", back_populates="hourly_report")
    custom_task_claims = db.relationship("CustomTask", back_populates="hourly_report")

    __table_args__ = (db.UniqueConstraint("session_id", "slot_id", name="uq_hourly_report_session_slot"),)

    def to_dict(self):
        return {
            "id": self.id,
            "session_id": self.session_id,
            "slot_id": self.slot_id,
            "note": self.note,
        }


class RegularTask(db.Model):
    __tablename__ = "regular_task"
    id = db.Column(db.Integer, primary_key=True)
    title_zh = db.Column(db.String(128), nullable=False)
    title_en = db.Column(db.String(128), nullable=True)
    description = db.Column(db.Text, nullable=True)
    frequency = db.Column(db.String(16), nullable=False)  # daily|weekly|monthly|unlimited
    interval = db.Column(db.Integer, nullable=False, default=1)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    reference_s3_key = db.Column(db.String(255), nullable=True)  # admin's "what to do" photo
    photo_required = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "title_zh": self.title_zh,
            "title_en": self.title_en,
            "description": self.description,
            "frequency": self.frequency,
            "interval": self.interval,
            "is_active": self.is_active,
            "reference_s3_key": self.reference_s3_key,
            "photo_required": self.photo_required,
        }


class TaskCompletion(db.Model):
    __tablename__ = "task_completion"
    id = db.Column(db.Integer, primary_key=True)
    regular_task_id = db.Column(db.Integer, db.ForeignKey("regular_task.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    session_id = db.Column(db.Integer, db.ForeignKey("attendance_session.id"), nullable=False)
    hourly_report_id = db.Column(db.Integer, db.ForeignKey("hourly_report.id"), nullable=True)
    slot_id = db.Column(db.Integer, db.ForeignKey("slot.id"), nullable=True)
    completed_at = db.Column(db.DateTime, nullable=False, default=local_now)
    period_key = db.Column(db.String(64), nullable=False)  # "u-<uuid4>" for unlimited tasks needs the room
    proof_s3_key = db.Column(db.String(255), nullable=True)  # student's completion photo

    regular_task = db.relationship("RegularTask")
    student = db.relationship("Student")
    hourly_report = db.relationship("HourlyReport", back_populates="task_completions")

    __table_args__ = (
        db.UniqueConstraint("regular_task_id", "period_key", name="uq_task_completion_period"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "regular_task_id": self.regular_task_id,
            "student_id": self.student_id,
            "completed_at": self.completed_at.isoformat(),
            "period_key": self.period_key,
            "proof_s3_key": self.proof_s3_key,
        }


class CustomTask(db.Model):
    __tablename__ = "custom_task"
    id = db.Column(db.Integer, primary_key=True)
    title_zh = db.Column(db.String(128), nullable=False)
    title_en = db.Column(db.String(128), nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("app_user.id"), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="open")  # open|claimed|done
    claimed_by = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    hourly_report_id = db.Column(db.Integer, db.ForeignKey("hourly_report.id"), nullable=True)
    event_date = db.Column(db.Date, nullable=True)  # set -> banners on sign-in from this date until done
    reference_s3_key = db.Column(db.String(255), nullable=True)  # admin's "what to do" photo
    photo_required = db.Column(db.Boolean, nullable=False, default=False)
    proof_s3_key = db.Column(db.String(255), nullable=True)  # student's completion photo

    claimer = db.relationship("Student")
    hourly_report = db.relationship("HourlyReport", back_populates="custom_task_claims")

    def to_dict(self):
        return {
            "id": self.id,
            "title_zh": self.title_zh,
            "title_en": self.title_en,
            "description": self.description,
            "status": self.status,
            "claimed_by": self.claimed_by,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "reference_s3_key": self.reference_s3_key,
            "photo_required": self.photo_required,
            "proof_s3_key": self.proof_s3_key,
        }


class TimecardUpload(db.Model):
    __tablename__ = "timecard_upload"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    period_label = db.Column(db.String(16), nullable=False)
    s3_key = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=local_now)
    cadence = db.Column(db.String(16), nullable=False)  # per_session|weekly|monthly

    student = db.relationship("Student")

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "period_label": self.period_label,
            "uploaded_at": self.uploaded_at.isoformat(),
            "cadence": self.cadence,
        }


class NotificationLog(db.Model):
    __tablename__ = "notification_log"
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(32), nullable=False)  # committed|leave_requested|slot_open|selection_open|closing_warning|no_show
    target = db.Column(db.String(16), nullable=False)  # group|individual|overseer
    related_type = db.Column(db.String(32), nullable=True)
    related_id = db.Column(db.Integer, nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    sent_flag = db.Column(db.Boolean, nullable=False, default=False)

    __table_args__ = (
        db.UniqueConstraint("type", "related_type", "related_id", "target", name="uq_notification_dedup"),
    )


class UiString(db.Model):
    __tablename__ = "ui_string"
    key = db.Column(db.String(128), primary_key=True)
    zh = db.Column(db.Text, nullable=False)
    en = db.Column(db.Text, nullable=False)


class AppSetting(db.Model):
    """Runtime-tunable config (solver weights, floor hours, cadences) so the
    overseer can adjust them from the UI without a redeploy (CLAUDE.md #6, #17:
    'config over hard-coding'). Falls back to Config defaults when absent."""
    __tablename__ = "app_setting"
    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.JSON, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=local_now, onupdate=local_now)
