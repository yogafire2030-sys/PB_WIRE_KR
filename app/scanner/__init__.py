"""SEPA 스크리너(마크 미너비니 Trend Template × Stocktwits 버즈) — /scanner 블루프린트.

데이터: Yahoo Finance 1년 일봉으로 S&P 500 + 나스닥 100 전 종목을 서버가 주기적으로 계산.
"""
import atexit

from flask import Blueprint, current_app, jsonify, render_template, request

from . import db
from .filters import POST_PARAMS, SELECTS, sanitize
from .security import RateLimiter, csrf_protect, get_csrf_token, rate_limited
from .services import minervini, stocktwits
from .services.engine import ScanEngine
from .services.minervini import CHECK_DEFS

bp = Blueprint("scanner", __name__, url_prefix="/scanner")

_ROW_FIELDS = (
    "ticker", "company", "exchange", "price", "change", "sma50", "sma150", "sma200", "sma50_pct", "sma150_pct",
    "sma200_pct", "high52", "low52", "from_high", "from_low", "perf_q", "perf_h", "perf_y", "rsi",
    "avg_volume", "rel_volume", "rs_rank", "checks", "passed", "total",
)
MAX_ROWS = 600


def _engine() -> ScanEngine:
    return current_app.extensions["scan_engine"]


def init_app(app) -> None:
    cfg = app.config
    timeout = (cfg["SCANNER_CONNECT_TIMEOUT"], cfg["SCANNER_READ_TIMEOUT"])
    app.extensions["scan_engine"] = ScanEngine(refresh_sec=cfg["SCAN_REFRESH_MIN"] * 60,
                                               workers=cfg["SCAN_WORKERS"], timeout=timeout)
    stocktwits.configure(ttl=cfg["STOCKTWITS_CACHE_TTL"], max_pages=cfg["STOCKTWITS_MAX_PAGES"],
                         workers=cfg["STOCKTWITS_MAX_WORKERS"], timeout=timeout)
    atexit.register(stocktwits.shutdown)
    db.init(cfg["SCANNER_DB_PATH"])
    app.extensions["scanner_rate_limiter"] = RateLimiter(cfg["SCANNER_RATE_LIMIT_PER_MIN"])
    app.register_blueprint(bp)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@bp.get("/")
def index():
    _engine().ensure_fresh()  # 첫 방문 시 계산 시작
    return render_template("scanner.html", selects=SELECTS, post_params=POST_PARAMS,
                           checks=CHECK_DEFS, csrf_token=get_csrf_token())


@bp.get("/api/csrf")
def api_csrf():
    """서버 재시작(무료 플랜 슬립 후 기동) 등으로 토큰이 바뀌었을 때 화면이 새 토큰을 받는 용도."""
    return jsonify(token=get_csrf_token())


@bp.get("/api/status")
def api_status():
    eng = _engine()
    eng.ensure_fresh()
    return jsonify(eng.status())


@bp.post("/api/screen")
@csrf_protect
@rate_limited
def api_screen():
    params = sanitize(_json_body().get("params", {}))
    eng = _engine()
    eng.ensure_fresh()
    status = eng.status()
    if not status["ready"]:
        return jsonify(preparing=True, status=status), 202

    rows = [r for r in eng.rows(params["universe"])
            if r["price"] > params["min_price"]
            and (params["min_volume"] == 0 or (r["avg_volume"] or 0) > params["min_volume"])]
    total = len(rows)
    rows = minervini.apply(rows, params)[:MAX_ROWS]
    out = []
    for r in rows:
        o = {k: r.get(k) for k in _ROW_FIELDS}
        for k, v in o.items():  # 표시용 반올림(전송량 축소)
            if isinstance(v, float):
                o[k] = round(v, 2)
        out.append(o)
    full = sum(1 for r in out if r["passed"] == r["total"])
    return jsonify(rows=out, total=total, full=full, status=status)


@bp.post("/api/buzz")
@csrf_protect
@rate_limited
def api_buzz():
    raw = _json_body().get("symbols", [])
    if not isinstance(raw, list):
        return jsonify(error="symbols는 배열이어야 합니다."), 400
    limit = current_app.config["STOCKTWITS_MAX_SYMBOLS"]
    symbols = []
    for s in raw[: limit * 2]:
        if isinstance(s, str):
            s = s.strip().upper()
            if stocktwits.valid_ticker(s) and s not in symbols:
                symbols.append(s)
    symbols = symbols[:limit]
    if not symbols:
        return jsonify(error="유효한 종목 코드가 없습니다."), 400

    results = stocktwits.analyze_many(symbols)
    trending = stocktwits.fetch_trending()
    try:
        db.save_snapshots(results)
        base = db.watcher_deltas(symbols)
    except Exception:
        current_app.logger.exception("snapshot save failed")
        base = {}
    for r in results:
        r["trending"] = r["symbol"] in trending
        w0 = base.get(r["symbol"])
        r["watchers_delta"] = (r["watchers"] - w0) if (w0 is not None and r.get("watchers") is not None) else None
    return jsonify(results=results)


@bp.get("/api/buzz/history/<symbol>")
@rate_limited
def api_buzz_history(symbol: str):
    symbol = symbol.upper()
    if not stocktwits.valid_ticker(symbol):
        return jsonify(error="잘못된 종목 코드입니다."), 400
    return jsonify(symbol=symbol, history=db.history(symbol))


@bp.errorhandler(403)
def _forbidden(_e):
    return jsonify(error="보안 토큰이 만료되었습니다. 페이지를 새로고침하세요.", code="csrf"), 403


@bp.errorhandler(405)
def _not_allowed(_e):
    return jsonify(error="허용되지 않는 메서드입니다."), 405
