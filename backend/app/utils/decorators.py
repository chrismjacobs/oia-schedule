from functools import wraps
from flask import jsonify, request, current_app, redirect, url_for
from flask_login import current_user


def login_required_api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "unauthenticated"}), 401
        return fn(*args, **kwargs)
    return wrapper


def overseer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "unauthenticated"}), 401
        if current_user.role != "overseer":
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper


def page_login_required(fn):
    """For Jinja page routes (not the JSON API) — redirects to /login instead
    of returning a JSON 401."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("pages.login_page", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def page_overseer_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("pages.login_page", next=request.path))
        if current_user.role != "overseer":
            return redirect(url_for("pages.home"))
        return fn(*args, **kwargs)
    return wrapper


def tick_token_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Tick-Token") or request.args.get("token")
        if not token or token != current_app.config["TICK_TOKEN"]:
            return jsonify({"error": "forbidden"}), 403
        return fn(*args, **kwargs)
    return wrapper
