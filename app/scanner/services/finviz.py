"""Finviz 스크리너 조회 및 파싱.

Custom 뷰(v=152)로 미너비니 판정에 필요한 컬럼을 한 번에 받아옵니다.
컬럼은 헤더 이름으로 매핑하므로 Finviz의 컬럼 순서 변화에도 견고합니다.
"""
import re
import threading
import time
from typing import Optional
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .cache import TTLCache
from .http_client import describe_error, get_session

SCREENER_URL = "https://finviz.com/screener.ashx"
QUOTE_URL = "https://finviz.com/quote.ashx"
PAGE_SIZE = 20

# No, Ticker, Company, Sector, Industry, Market Cap, P/E, EPS this Y, EPS Q/Q, Sales Q/Q,
# Inst Own, ROE, Perf Week/Month/Quarter/Half/Year, SMA50, SMA200, 52W High, 52W Low,
# RSI, Avg Volume, Rel Volume, Price, Change, Volume, Earnings
CUSTOM_COLUMNS = "0,1,2,3,4,6,7,17,22,23,28,33,42,43,44,45,46,53,54,57,58,59,63,64,65,66,67,68"

# Finviz 헤더명 → 내부 키
HEADER_MAP = {
    "ticker": "ticker", "company": "company", "sector": "sector", "industry": "industry",
    "market cap": "market_cap", "p/e": "pe", "eps this y": "eps_this_y", "eps q/q": "eps_qoq",
    "sales q/q": "sales_qoq", "inst own": "inst_own", "roe": "roe",
    "perf week": "perf_w", "perf month": "perf_m", "perf quart": "perf_q", "perf quarter": "perf_q",
    "perf half": "perf_h", "perf half y": "perf_h", "perf year": "perf_y",
    "sma50": "sma50", "sma200": "sma200", "52w high": "high52", "52w low": "low52",
    "rsi": "rsi", "rsi (14)": "rsi", "avg volume": "avg_volume", "rel volume": "rel_volume",
    "price": "price", "change": "change", "volume": "volume", "earnings": "earnings",
}
NUMERIC_KEYS = frozenset({
    "pe", "eps_this_y", "eps_qoq", "sales_qoq", "inst_own", "roe", "perf_w", "perf_m", "perf_q",
    "perf_h", "perf_y", "sma50", "sma200", "high52", "low52", "rsi", "rel_volume", "price", "change",
})
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_TOTAL_RE = re.compile(r"/\s*([\d,]+)\s*Total", re.I)

_cache = TTLCache(maxsize=64, ttl=600)
# 여러 사용자가 동시에 스캔해도 Finviz 조회는 한 번에 하나만(차단·서버 부하 방지).
# 같은 조건이면 먼저 끝난 결과를 캐시로 함께 사용
_scan_lock = threading.Lock()
SCAN_WAIT_SEC = 90


class FinvizError(Exception):
    pass


def configure(ttl: int) -> None:
    global _cache
    _cache = TTLCache(maxsize=64, ttl=ttl)


def build_url(codes: list, start: int = 1, view: int = 152) -> str:
    params = {"v": str(view)}
    if codes:
        params["f"] = ",".join(codes)
    if view == 152:
        params["c"] = CUSTOM_COLUMNS
    if start > 1:
        params["r"] = str(start)
    return f"{SCREENER_URL}?{urlencode(params, safe=',')}"


def quote_url(ticker: str) -> str:
    return f"{QUOTE_URL}?{urlencode({'t': ticker})}"


def to_number(text: str) -> Optional[float]:
    if text is None:
        return None
    t = text.strip().replace(",", "")
    if not t or t in {"-", "—", "N/A"}:
        return None
    if t.endswith("%"):
        t = t[:-1]
    mult = 1.0
    if t and t[-1].upper() in _SUFFIX:
        mult = _SUFFIX[t[-1].upper()]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


_HDR_CLEAN = re.compile(r"[^a-z0-9/(). ]")
_QUOTE_T = re.compile(r"quote\.ashx\?t=([A-Za-z0-9.\-]{1,10})")


def _cells(tr):
    return tr.find_all(["td", "th"], recursive=False)


def _norm_header(text: str) -> str:
    return " ".join(_HDR_CLEAN.sub("", text.lower()).split())


def _own_rows(table):
    """표에 직접 속한 행만 반환 (thead/tbody가 몇 개든, 중첩 표는 제외)."""
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _find_table(soup):
    """1순위 table.screener_table, 2순위 'Ticker' 헤더를 가진 가장 안쪽 표."""
    for t in soup.find_all("table", class_="screener_table"):
        if not t.find("table"):
            return t
    candidates = []
    for tr in soup.find_all("tr"):
        texts = {_norm_header(c.get_text(" ", strip=True)) for c in _cells(tr)}
        if "ticker" in texts and len(texts) >= 3:
            candidates.append(tr.find_parent("table"))
    # 중첩 표가 없는(가장 안쪽) 표 우선
    for t in candidates:
        if t is not None and not t.find("table"):
            return t
    return candidates[0] if candidates else None


def _ticker_of(cell, text: str) -> str:
    tk = cell.get("data-boxover-ticker") or ""
    if not tk:
        a = cell.find("a", href=True)
        m = _QUOTE_T.search(a["href"]) if a else None
        tk = m.group(1) if m else text
    return tk.strip().upper()


def parse_screener(html: str):
    """HTML에서 (rows, total) 추출. 표를 찾지 못하면 ([], total)."""
    soup = BeautifulSoup(html, "lxml")
    total = 0
    m = _TOTAL_RE.search(soup.get_text(" ", strip=True))
    if m:
        total = int(m.group(1).replace(",", ""))

    table = _find_table(soup)
    if table is None:
        return [], total
    trs = _own_rows(table)
    header_idx, keys = None, []
    for i, tr in enumerate(trs):
        norms = [_norm_header(c.get_text(" ", strip=True)) for c in _cells(tr)]
        if "ticker" in norms:
            header_idx, keys = i, [HEADER_MAP.get(n) for n in norms]
            break
    if header_idx is None:
        return [], total

    rows = []
    for tr in trs[header_idx + 1:]:
        cells = _cells(tr)
        if len(cells) < 2:
            continue
        row = {}
        # 셀 수가 헤더와 다르면 앞에서부터 가능한 만큼만 매핑
        for key, cell in zip(keys, cells):
            if not key:
                continue
            txt = cell.get_text(strip=True)
            if key == "ticker":
                row[key] = _ticker_of(cell, txt)
            elif key in NUMERIC_KEYS:
                row[key] = to_number(txt)
            elif key in ("market_cap", "avg_volume", "volume"):
                row[key] = to_number(txt)
                row[key + "_text"] = txt
            else:
                row[key] = txt
        tk = row.get("ticker", "")
        if tk and re.fullmatch(r"[A-Z0-9.\-]{1,10}", tk):
            rows.append(row)
    if not total:
        total = len(rows)
    return rows, total


def screen(codes: list, max_pages: int = 15, delay: float = 0.6, timeout=(5, 20)) -> dict:
    """필터 코드로 스크리닝하여 모든 페이지(최대 max_pages) 결과를 반환."""
    cache_key = (tuple(codes), max_pages)
    cached = _cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}
    if not _scan_lock.acquire(timeout=SCAN_WAIT_SEC):
        raise FinvizError("다른 사용자의 스크리닝이 진행 중입니다. 잠시 후 다시 시도하세요.")
    try:
        cached = _cache.get(cache_key)  # 기다리는 동안 같은 조건 결과가 생겼을 수 있음
        if cached is not None:
            return {**cached, "cached": True}
        return _screen(codes, cache_key, max_pages, delay, timeout)
    finally:
        _scan_lock.release()


def _screen(codes, cache_key, max_pages, delay, timeout) -> dict:
    session = get_session()
    seen, results, total, pages = set(), [], 0, 0
    start = 1
    while pages < max_pages:
        url = build_url(codes, start)
        try:
            resp = session.get(url, timeout=timeout, headers={"Referer": "https://finviz.com/screener.ashx"})
        except Exception as exc:  # 네트워크 오류는 사용자 메시지로 변환
            raise FinvizError(f"Finviz {describe_error(exc)}") from None
        if resp.status_code == 429:
            raise FinvizError("Finviz 요청 한도에 도달했습니다. 잠시 후 다시 시도하세요.")
        if resp.status_code == 403:
            raise FinvizError("Finviz가 서버 접속을 차단했습니다(HTTP 403). 잠시 후 다시 시도하세요.")
        if resp.status_code != 200:
            raise FinvizError(f"Finviz 응답 오류 (HTTP {resp.status_code})")
        html = resp.text
        resp.close()
        rows, page_total = parse_screener(html)
        if not rows and page_total and not results:
            raise FinvizError(
                f"Finviz 결과 {page_total}개를 찾았지만 표를 읽지 못했습니다. "
                "Finviz 화면 구조가 바뀌었을 수 있습니다.")
        pages += 1
        total = max(total, page_total)
        new = 0
        for r in rows:
            if r["ticker"] not in seen:
                seen.add(r["ticker"])
                results.append(r)
                new += 1
        if new == 0 or len(rows) < PAGE_SIZE or len(results) >= total:
            break
        start += PAGE_SIZE
        time.sleep(delay)

    data = {
        "rows": results,
        "total": total,
        "fetched": len(results),
        "truncated": total > len(results),
        "url": build_url(codes, 1, view=111),
    }
    _cache.set(cache_key, data)
    return {**data, "cached": False}
