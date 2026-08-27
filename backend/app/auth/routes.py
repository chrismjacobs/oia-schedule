import re
from datetime import datetime

from flask import jsonify, request
from flask_login import login_user, logout_user, current_user

from app.auth import bp
from app.extensions import db
from app.models import User, Student, Semester
from app.utils.identity import assign_token
from app.utils.decorators import login_required_api

STUDENT_ID_RE = re.compile(r"^[0-9]{8}$")


@bp.get("/invite/<token>")
def get_invite(token):
    user = User.query.filter_by(invite_token=token, invite_accepted_at=None).first()
    if not user:
        return jsonify({"error": "invalid_or_used_invite"}), 404
    return jsonify({"email": user.email, "role": user.role})


@bp.post("/register")
def register():
    data = request.get_json(force=True) or {}
    token = data.get("token", "")
    chinese_name = (data.get("chinese_name") or "").strip()
    english_name = (data.get("english_name") or "").strip()
    student_id = (data.get("student_id") or "").strip()
    password = data.get("password") or ""

    user = User.query.filter_by(invite_token=token, invite_accepted_at=None).first()
    if not user:
        return jsonify({"error": "invalid_or_used_invite"}), 404

    if not chinese_name and not english_name:
        return jsonify({"error": "name_required", "message": "Provide at least one of Chinese/English name"}), 400
    if not STUDENT_ID_RE.match(student_id):
        return jsonify({"error": "invalid_student_id", "message": "Student ID must be exactly 8 digits"}), 400
    if len(password) < 8:
        return jsonify({"error": "weak_password", "message": "Password must be at least 8 characters"}), 400
    if Student.query.filter_by(student_id=student_id).first():
        return jsonify({"error": "student_id_taken"}), 409

    semester = Semester.query.filter_by(is_active=True).first()
    if not semester:
        return jsonify({"error": "no_active_semester", "message": "Ask the overseer to open a semester first"}), 400

    colour, shape = assign_token(semester.id)
    student = Student(
        semester_id=semester.id,
        chinese_name=chinese_name,
        english_name=english_name,
        student_id=student_id,
        colour=colour,
        shape=shape,
    )
    db.session.add(student)
    db.session.flush()

    user.student_id = student.id
    user.set_password(password)
    user.invite_accepted_at = datetime.utcnow()
    db.session.commit()

    login_user(user)
    return jsonify(user.to_dict()), 201


@bp.post("/login")
def login():
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    user = User.query.filter(db.func.lower(User.email) == email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "invalid_credentials"}), 401
    login_user(user, remember=True)
    return jsonify(user.to_dict())


@bp.post("/logout")
@login_required_api
def logout():
    logout_user()
    return jsonify({"ok": True})


@bp.get("/me")
def me():
    if not current_user.is_authenticated:
        return jsonify(None)
    return jsonify(current_user.to_dict())
