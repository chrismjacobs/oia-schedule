from flask import Blueprint

bp = Blueprint("tasks", __name__, url_prefix="/api/tasks")

from app.tasks import routes  # noqa: E402,F401
