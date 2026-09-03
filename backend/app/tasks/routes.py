from datetime import date as date_cls

from flask import jsonify, request
from flask_login import current_user

from app.tasks import bp
from app.extensions import db
from app.models import RegularTask, CustomTask, REGULAR_TASK_FREQUENCIES
from app.utils.decorators import overseer_required, login_required_api
from app.utils.s3 import upload_object, presigned_view_url


@bp.get("/regular")
@overseer_required
def list_regular():
    rows = RegularTask.query.order_by(RegularTask.title_en).all()
    return jsonify([r.to_dict() for r in rows])


@bp.get("/regular/active")
@login_required_api
def list_regular_active():
    """Read-only reference list for the student Tasks page — what each
    regular task involves, not an action screen. Ticking happens at
    sign-out (CLAUDE.md #10)."""
    rows = RegularTask.query.filter_by(is_active=True).order_by(RegularTask.title_en).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("/regular")
@overseer_required
def create_regular():
    data = request.get_json(force=True) or {}
    if not data.get("title_zh") or not data.get("frequency"):
        return jsonify({"error": "missing_fields"}), 400
    if data["frequency"] not in REGULAR_TASK_FREQUENCIES:
        return jsonify({"error": "invalid_frequency"}), 400
    t = RegularTask(
        title_zh=data["title_zh"], title_en=data.get("title_en"),
        description=data.get("description"), frequency=data["frequency"],
        interval=int(data.get("interval", 1)), photo_required=bool(data.get("photo_required", False)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@bp.patch("/regular/<int:task_id>")
@overseer_required
def update_regular(task_id):
    t = RegularTask.query.get_or_404(task_id)
    data = request.get_json(force=True) or {}
    if "frequency" in data and data["frequency"] not in REGULAR_TASK_FREQUENCIES:
        return jsonify({"error": "invalid_frequency"}), 400
    for field in ("title_zh", "title_en", "description", "frequency", "interval", "is_active", "photo_required"):
        if field in data:
            setattr(t, field, data[field])
    db.session.commit()
    return jsonify(t.to_dict())


@bp.post("/regular/<int:task_id>/reference-photo")
@overseer_required
def upload_regular_reference(task_id):
    t = RegularTask.query.get_or_404(task_id)
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.reference_s3_key = upload_object(request.files["file"], f"tasks/regular/{task_id}/reference")
    db.session.commit()
    return jsonify(t.to_dict())


@bp.get("/regular/<int:task_id>/reference-photo-url")
@login_required_api
def get_regular_reference_url(task_id):
    t = RegularTask.query.get_or_404(task_id)
    if not t.reference_s3_key:
        return jsonify({"error": "no_photo"}), 404
    return jsonify({"url": presigned_view_url(t.reference_s3_key)})


@bp.get("/custom")
@login_required_api
def list_custom():
    status = request.args.get("status")
    q = CustomTask.query
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(CustomTask.id.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@bp.post("/custom")
@overseer_required
def create_custom():
    data = request.get_json(force=True) or {}
    if not data.get("title_zh"):
        return jsonify({"error": "missing_fields"}), 400
    event_date = date_cls.fromisoformat(data["event_date"]) if data.get("event_date") else None
    t = CustomTask(
        title_zh=data["title_zh"], title_en=data.get("title_en"),
        description=data.get("description"), created_by=current_user.id, status="open",
        event_date=event_date, photo_required=bool(data.get("photo_required", False)),
    )
    db.session.add(t)
    db.session.commit()
    return jsonify(t.to_dict()), 201


@bp.patch("/custom/<int:task_id>")
@overseer_required
def update_custom(task_id):
    t = CustomTask.query.get_or_404(task_id)
    data = request.get_json(force=True) or {}
    if "event_date" in data:
        t.event_date = date_cls.fromisoformat(data["event_date"]) if data["event_date"] else None
    for field in ("title_zh", "title_en", "description", "status", "photo_required"):
        if field in data:
            setattr(t, field, data[field])
    db.session.commit()
    return jsonify(t.to_dict())


@bp.post("/custom/<int:task_id>/reference-photo")
@overseer_required
def upload_custom_reference(task_id):
    t = CustomTask.query.get_or_404(task_id)
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.reference_s3_key = upload_object(request.files["file"], f"tasks/custom/{task_id}/reference")
    db.session.commit()
    return jsonify(t.to_dict())


@bp.get("/custom/<int:task_id>/reference-photo-url")
@login_required_api
def get_custom_reference_url(task_id):
    t = CustomTask.query.get_or_404(task_id)
    if not t.reference_s3_key:
        return jsonify({"error": "no_photo"}), 404
    return jsonify({"url": presigned_view_url(t.reference_s3_key)})


@bp.post("/custom/<int:task_id>/proof-photo")
@login_required_api
def upload_custom_proof(task_id):
    """Student's completion evidence — separate key from the admin's reference photo."""
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    t = CustomTask.query.get_or_404(task_id)
    if t.claimed_by != current_user.student_id:
        return jsonify({"error": "not_your_task"}), 403
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    t.proof_s3_key = upload_object(request.files["file"], f"tasks/custom/{task_id}/proof")
    db.session.commit()
    return jsonify(t.to_dict())
