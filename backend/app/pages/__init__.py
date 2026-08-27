from flask import Blueprint

bp = Blueprint("pages", __name__)

from app.pages import routes  # noqa: E402,F401
