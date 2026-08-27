from flask import Blueprint

bp = Blueprint("schedule", __name__, url_prefix="/api/schedule")

from app.schedule import routes  # noqa: E402,F401
