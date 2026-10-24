"""라우트 — 화면 1개 + JSON API 4개."""
from flask import Blueprint, abort, current_app, jsonify, render_template, request

from .services.market import INDICES

bp = Blueprint("main", __name__)


def _feed():
    return current_app.extensions["feed"]


@bp.get("/")
def index():
    cfg = current_app.config
    channels = [{"key": k, **v} for k, v in cfg["CHANNELS"].items()]
    return render_template("index.html", channels=channels,
                           default_channel=cfg["DEFAULT_CHANNEL"],
                           indices=[{"key": k, "name": n} for k, n, *_ in INDICES],
                           retention_h=cfg["RETENTION_HOURS"],
                           poll_ms=5000)


@bp.get("/api/news")
def api_news():
    channel = request.args.get("channel", current_app.config["DEFAULT_CHANNEL"])
    if channel not in current_app.config["CHANNELS"]:  # 화이트리스트 검증
        abort(400)
    since = request.args.get("since", "0")
    if not since.isdigit() or len(since) > 12:
        abort(400)
    return jsonify(_feed().delta(channel, int(since)))


@bp.get("/api/market")
def api_market():
    return jsonify(current_app.extensions["market"].snapshot())


@bp.get("/api/market/history")
def api_market_history():
    return jsonify(current_app.extensions["market"].history())


@bp.get("/api/status")
def api_status():
    return jsonify(_feed().status())


@bp.app_errorhandler(400)
def _bad(_e):
    return jsonify(error="bad request"), 400


@bp.app_errorhandler(404)
def _nf(_e):
    return jsonify(error="not found"), 404


@bp.app_errorhandler(500)
def _ise(_e):
    return jsonify(error="internal error"), 500
