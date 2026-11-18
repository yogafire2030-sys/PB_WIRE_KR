"""Stocktwits 언급량(버즈) 분석.

- 심볼 스트림을 최신→과거 순으로 페이지네이션하며 최근 48시간 메시지를 수집
- 최근 24시간 vs 직전 24시간 언급 건수, 강세/약세 비율, 관심(Watchlist) 수를 계산
- 결과는 SQLite 스냅샷으로 저장되어 시간 흐름에 따른 증가 추세를 확인할 수 있음
"""
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from .cache import TTLCache
from .http_client import describe_error, get_session

STREAM_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"
TRENDING_URL = "https://api.stocktwits.com/api/2/trending/symbols.json"
SYMBOL_PAGE = "https://stocktwits.com/symbol/{sym}"

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z]{1,2})?$")

_cache = TTLCache(maxsize=512, ttl=600)
_trend_cache = TTLCache(maxsize=1, ttl=600)
_executor: Optional[ThreadPoolExecutor] = None
_settings = {"max_pages": 4, "timeout": (5, 15)}


def configure(ttl: int, max_pages: int, workers: int, timeout) -> None:
    global _cache, _trend_cache, _executor
    _cache = TTLCache(maxsize=512, ttl=ttl)
    _trend_cache = TTLCache(maxsize=1, ttl=ttl)
    _settings.update(max_pages=max_pages, timeout=timeout)
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stwits")


def shutdown() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def valid_ticker(sym: str) -> bool:
    return isinstance(sym, str) and bool(TICKER_RE.match(sym))


def _parse_time(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def fetch_trending() -> set:
    cached = _trend_cache.get("t")
    if cached is not None:
        return cached
    result = set()
    try:
        resp = get_session().get(TRENDING_URL, timeout=_settings["timeout"])
        if resp.status_code == 200:
            for s in resp.json().get("symbols", [])[:100]:
                sym = str(s.get("symbol", "")).upper()
                if valid_ticker(sym):
                    result.add(sym)
        resp.close()
    except Exception:
        pass
    _trend_cache.set("t", frozenset(result))
    return frozenset(result)


def _analyze(sym: str) -> dict:
    now = datetime.now(timezone.utc)
    t24, t48 = now - timedelta(hours=24), now - timedelta(hours=48)
    session = get_session()
    api_sym = sym.replace("-", ".")  # Finviz BRK-B → Stocktwits BRK.B
    out = {
        "symbol": sym, "ok": False, "error": None, "watchers": None, "title": None,
        "msgs_24h": 0, "msgs_prev24h": 0, "bull": 0, "bear": 0,
        "coverage_hours": 0.0, "saturated": False, "change_pct": None,
        "url": SYMBOL_PAGE.format(sym=api_sym),
    }
    cursor_max = None
    oldest = None
    pages = 0
    reached_48h = False
    while pages < _settings["max_pages"]:
        params = {"max": cursor_max} if cursor_max else None
        try:
            resp = session.get(STREAM_URL.format(sym=api_sym), params=params, timeout=_settings["timeout"])
        except Exception as exc:
            out["error"] = describe_error(exc)
            return out
        status = resp.status_code
        if status != 200:
            resp.close()
            out["error"] = {404: "심볼 없음", 429: "요청 한도 초과", 403: "접근 차단됨"}.get(status, f"HTTP {status}")
            return out
        try:
            data = resp.json()
        except ValueError:
            out["error"] = "응답 파싱 실패"
            return out
        finally:
            resp.close()

        if pages == 0:
            sym_info = data.get("symbol") or {}
            out["watchers"] = sym_info.get("watchlist_count")
            out["title"] = sym_info.get("title")
        msgs = data.get("messages") or []
        for m in msgs:
            ts = _parse_time(m.get("created_at", ""))
            if ts is None:
                continue
            oldest = ts if oldest is None or ts < oldest else oldest
            if ts >= t24:
                out["msgs_24h"] += 1
                senti = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                if senti == "Bullish":
                    out["bull"] += 1
                elif senti == "Bearish":
                    out["bear"] += 1
            elif ts >= t48:
                out["msgs_prev24h"] += 1
            else:
                reached_48h = True
        pages += 1
        cursor = data.get("cursor") or {}
        if reached_48h or not msgs or not cursor.get("more") or not cursor.get("max"):
            break
        cursor_max = cursor["max"]

    more_exists = pages >= _settings["max_pages"] and not reached_48h
    out["coverage_hours"] = round((now - oldest).total_seconds() / 3600, 1) if oldest else 0.0
    # 페이지 한도 안에서 48시간을 다 못 채웠으면 언급량이 매우 많음(포화)
    out["saturated"] = more_exists and out["coverage_hours"] < 48
    if out["saturated"] and out["coverage_hours"] < 24:
        out["change_pct"] = None  # 24시간도 못 채움 → 비교 불가, '폭증' 표시
    elif not out["saturated"]:
        prev = out["msgs_prev24h"]
        cur = out["msgs_24h"]
        out["change_pct"] = round((cur - prev) / prev * 100, 1) if prev else (None if cur == 0 else 100.0)
    out["ok"] = True
    return out


def analyze(sym: str) -> dict:
    cached = _cache.get(sym)
    if cached is not None:
        return {**cached, "cached": True}
    res = _analyze(sym)
    if res["ok"]:
        _cache.set(sym, res)
    return {**res, "cached": False}


def analyze_many(symbols: list) -> list:
    if _executor is None:
        return [analyze(s) for s in symbols]
    # map은 입력 순서를 유지
    return list(_executor.map(analyze, symbols))
