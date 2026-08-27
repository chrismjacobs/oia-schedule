from flask import render_template, redirect, url_for, request
from flask_login import current_user

from app.pages import bp
from app.utils.decorators import page_login_required, page_overseer_required


@bp.get("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("pages.home"))
    return render_template("login.html", active_nav=None)


@bp.get("/register")
@bp.get("/register/<token>")
def register_page(token=None):
    if current_user.is_authenticated:
        return redirect(url_for("pages.home"))
    return render_template("register.html", active_nav=None, invite_token=token or "")


@bp.get("/")
@page_login_required
def home():
    return redirect(url_for("pages.day_page" if current_user.role == "overseer" else "pages.schedule_page"))


@bp.get("/day")
@bp.get("/day/<date_str>")
@page_overseer_required
def day_page(date_str=None):
    return render_template("day.html", active_nav="day", initial_date=date_str or "")


@bp.get("/week")
@page_overseer_required
def week_page():
    return render_template("week.html", active_nav="week", wide_page=True)


@bp.get("/dashboard")
@page_overseer_required
def dashboard_page():
    return render_template("dashboard.html", active_nav="dashboard", wide_page=True)


@bp.get("/draft")
@page_overseer_required
def draft_page():
    return render_template("draft.html", active_nav="draft", wide_page=True)


@bp.get("/setup")
@page_overseer_required
def setup_page():
    return render_template("setup.html", active_nav="setup", wide_page=True)


@bp.get("/availability")
@page_login_required
def availability_page():
    return render_template("availability.html", active_nav="availability", wide_page=True)


@bp.get("/schedule")
@page_login_required
def schedule_page():
    return render_template("schedule.html", active_nav="schedule")


@bp.get("/attendance")
@page_login_required
def attendance_page():
    return render_template("attendance.html", active_nav="attendance")


@bp.get("/leave")
@page_login_required
def leave_page():
    return render_template("leave.html", active_nav="leave")


@bp.get("/tasks")
@page_login_required
def tasks_page():
    return render_template("tasks.html", active_nav="tasks")


@bp.get("/timecards")
@page_login_required
def timecards_page():
    return render_template("timecards.html", active_nav="timecards")
