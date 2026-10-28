"""Factor Factory — 롤링 멀티팩터 모델 대시보드 (Flask)."""
from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import secrets

from flask import Flask, Response, abort, jsonify, render_template, request, session

import config
from core import model, pipeline, report, store
from core.jobs import JobRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _QuietPolling(logging.Filter):
    """1~2초마다 오는 상태 확인 요청은 콘솔 로그에서 제외."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/status" not in record.getMessage()


logging.getLogger("werkzeug").addFilter(_QuietPolling())

CSP = ("default-src 'self'; "
       "script-src 'self' https://cdnjs.cloudflare.com; "
       "style-src 'self' https://cdn.jsdelivr.net; "
       "font-src https://cdn.jsdelivr.net; "
       "img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")


def _target_arg(value: str | None) -> str:
    return value if value in config.TARGETS else config.DEFAULT_TARGET


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("FACTOR_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=16 * 1024,
        JSON_AS_ASCII=False,
    )
    store.init_db()
    runner = JobRunner()
    demo_default = os.environ.get("FACTOR_DEMO") == "1"

    def csrf_token() -> str:
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def check_csrf() -> None:
        sent = request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            abort(403)

    @app.after_request
    def security_headers(resp: Response) -> Response:
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index():
        target = _target_arg(request.args.get("target"))
        data = report.dashboard(target)
        settings = pipeline.saved_settings(target).to_dict()
        return render_template(
            "index.html", data=data, target=target, targets=config.TARGETS,
            settings=settings, csrf=csrf_token(), static_site=False, static_prefix="/static/",
            links={k: f"/?target={k}" for k in config.TARGETS}, csv_href=f"/export.csv?target={target}",
            archive_href="/archive",
            demo=(data["source"] == "demo") if data else demo_default)

    @app.get("/archive")
    def archive_page():
        return render_template(
            "archive.html", a=report.archive(), targets=config.TARGETS, static_site=False,
            static_prefix="/static/", links={k: f"/?target={k}" for k in config.TARGETS},
            archive_href="/archive", target=None, updated="")

    @app.post("/api/update")
    def api_update():
        check_csrf()
        body = request.get_json(silent=True) or {}
        target = body.get("target")
        settings = model.Settings.from_dict(body.get("settings"))
        demo = bool(body.get("demo", False))
        if body.get("all"):
            jobs = [(k, settings if k == target else pipeline.saved_settings(k)) for k in config.TARGETS]
        elif target in config.TARGETS:
            jobs = [(target, settings)]
        else:
            return jsonify(ok=False, error="잘못된 대상입니다."), 400
        if not runner.start(jobs, demo):
            return jsonify(ok=False, error="이미 업데이트가 진행 중입니다."), 409
        return jsonify(ok=True), 202

    @app.get("/api/status")
    def api_status():
        return jsonify(runner.status())

    @app.get("/export.csv")
    def export_csv():
        target = _target_arg(request.args.get("target"))
        rows = report.today_csv_rows(target)
        if not rows:
            abort(404)
        buf = io.StringIO()
        buf.write("﻿")  # 엑셀 한글 깨짐 방지
        csv.writer(buf).writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8", headers={
            "Content-Disposition": f"attachment; filename=factor_{target}_{rows[1][0]}.csv"})

    @app.errorhandler(403)
    def forbidden(_):
        return jsonify(ok=False, error="요청이 거부되었습니다. 페이지를 새로고침하세요."), 403

    return app


app = create_app()

if __name__ == "__main__":
    host = os.environ.get("FACTOR_HOST", "127.0.0.1")
    port = int(os.environ.get("FACTOR_PORT", "5000"))
    app.run(host=host, port=port, debug=False, threaded=True)
