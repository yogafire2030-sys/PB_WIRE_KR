"""SEPA 스크리너(마크 미너비니 Trend Template × Stocktwits 버즈) — /scanner 블루프린트."""
import atexit

from flask import Blueprint, current_app, jsonify, render_template, request

from . import db
from .filters import FILTER_GROUPS, POST_PARAMS, sanitize_filters, sanitize_post_params
from .security import RateLimiter, csrf_protect, get_csrf_token, rate_limited
from .services import finviz, minervini, stocktwits
from .services.minervini import CHECK_DEFS

bp = Blueprint("scanner", __name__, url_prefix="/scanner")

_ROW_FIELDS = (
    "ticker", "company", "sector", "industry", "market_cap", "market_cap_text", "pe", "eps_this_y",
    "eps_qoq", "sales_qoq", "inst_own", "roe", "perf_w", "perf_m", "perf_q", "perf_h", "perf_y",
    "sma50", "sma200", "high52", "low52", "rsi", "avg_volume", "avg_volume_text", "rel_volume",
    "price", "change", "earnings", "checks", "passed", "total", "rs_rank",
)


def init_app(app) -> None:
    cfg = app.config
    timeout = (cfg["SCANNER_CONNECT_TIMEOUT"], cfg["SCANNER_READ_TIMEOUT"])
    finviz.configure(cfg["FINVIZ_CACHE_TTL"])
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
    return render_template("scanner.html", groups=FILTER_GROUPS, post_params=POST_PARAMS,
                           checks=CHECK_DEFS, csrf_token=get_csrf_token())


@bp.get("/api/csrf")
def api_csrf():
    """서버 재시작(무료 플랜 슬립 후 기동) 등으로 토큰이 바뀌었을 때 화면이 새 토큰을 받는 용도."""
    return jsonify(token=get_csrf_token())


@bp.post("/api/screen")
@csrf_protect
@rate_limited
def api_screen():
    body = _json_body()
    try:
        codes = sanitize_filters(body.get("filters", {}), body.get("custom", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    params = sanitize_post_params(body.get("params", {}))

    cfg = current_app.config
    try:
        data = finviz.screen(codes, max_pages=cfg["FINVIZ_MAX_PAGES"], delay=cfg["FINVIZ_PAGE_DELAY"],
                             timeout=(cfg["SCANNER_CONNECT_TIMEOUT"], cfg["SCANNER_READ_TIMEOUT"]))
    except finviz.FinvizError as exc:
        return jsonify(error=str(exc)), 502

    # 캐시 원본을 변경하지 않도록 행 복사 후 판정
    rows = minervini.apply([dict(r) for r in data["rows"]], params)
    rows = [{k: r.get(k) for k in _ROW_FIELDS} for r in rows]
    for r in rows:
        r["quote_url"] = finviz.quote_url(r["ticker"])
    return jsonify(rows=rows, total=data["total"], fetched=data["fetched"], truncated=data["truncated"],
                   cached=data["cached"], finviz_url=data["url"], codes=codes)


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
