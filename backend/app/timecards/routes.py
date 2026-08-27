from flask import jsonify, request
from flask_login import current_user

from app.timecards import bp
from app.extensions import db
from app.models import TimecardUpload
from app.utils.decorators import login_required_api, overseer_required
from app.utils.settings import get_timecard_cadence
from app.timecards.s3 import upload_timecard, presigned_view_url


@bp.get("")
@login_required_api
def my_uploads():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    rows = TimecardUpload.query.filter_by(student_id=current_user.student_id).order_by(
        TimecardUpload.uploaded_at.desc()
    ).all()
    return jsonify({"cadence": get_timecard_cadence(), "uploads": [r.to_dict() for r in rows]})


@bp.post("")
@login_required_api
def upload():
    if not current_user.student_id:
        return jsonify({"error": "students_only"}), 403
    if "file" not in request.files:
        return jsonify({"error": "file_required"}), 400
    period_label = request.form.get("period_label")
    if not period_label:
        return jsonify({"error": "period_label_required"}), 400

    key = upload_timecard(request.files["file"], current_user.student_id, period_label)
    row = TimecardUpload(
        student_id=current_user.student_id, period_label=period_label, s3_key=key,
        cadence=get_timecard_cadence(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@bp.get("/admin")
@overseer_required
def admin_list():
    student_id = request.args.get("student_id", type=int)
    q = TimecardUpload.query
    if student_id:
        q = q.filter_by(student_id=student_id)
    rows = q.order_by(TimecardUpload.uploaded_at.desc()).all()
    return jsonify([r.to_dict() for r in rows])


@bp.get("/<int:upload_id>/url")
@login_required_api
def get_view_url(upload_id):
    row = TimecardUpload.query.get_or_404(upload_id)
    if current_user.role != "overseer" and row.student_id != current_user.student_id:
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"url": presigned_view_url(row.s3_key)})
