from flask import Blueprint

bp = Blueprint("timecards", __name__, url_prefix="/api/timecards")

from app.timecards import routes  # noqa: E402,F401
