"""주요 지수·환율·금 실시간 시세 + 6개월 추이 서비스.

- 1차 소스: Yahoo Finance chart API / 2차(폴백): 네이버 증권 API(지수만)
- 백그라운드 스레드 1개가 '최근 화면에서 조회된 경우에만' 주기적으로 수집 → 접속자가 없으면 호출 0
- 시세는 병렬 수집, 결과는 불변 스냅샷(list)으로 교체 → 읽기 시 락 불필요
- 6개월 일봉은 30분마다만 갱신(시세 폴링과 분리 → 전송량 최소)
- 수집 실패 시 직전 값을 유지(stale 표시)해 화면이 비지 않음
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

# key, 표시명, Yahoo 심볼, 네이버 코드(None=폴백 없음), 네이버 구분, 표시 시간대
INDICES = (
    ("SPX", "S&P 500", "^GSPC", ".INX", "world", "America/New_York"),
    ("NDX", "NASDAQ 100", "^NDX", ".NDX", "world", "America/New_York"),
    ("N225", "NIKKEI 225", "^N225", ".N225", "world", "Asia/Tokyo"),
    ("CSI300", "CSI 300", "000300.SS", ".CSI300", "world", "Asia/Shanghai"),
    ("HSCEI", "HSCEI", "^HSCE", ".HSCE", "world", "Asia/Hong_Kong"),
    ("KOSPI", "KOSPI", "^KS11", "KOSPI", "domestic", "Asia/Seoul"),
    ("USDKRW", "USD/KRW", "KRW=X", None, "", "Asia/Seoul"),
    ("GOLD", "GOLD", "GC=F", None, "", "America/New_York"),
)

_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
ACTIVE_SECONDS = 120  # 마지막 조회 후 이 시간 동안만 수집
HISTORY_TTL = 1800    # 6개월 일봉 갱신 주기(초)


def _num(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(",", "").strip())


def fmt_time(ts: int, tz_name: str) -> str:
    """'2:12:10 PM JST' 형식(로캘 영향 없이 직접 조립)."""
    dt = datetime.fromtimestamp(ts, timezone.utc).astimezone(ZoneInfo(tz_name))
    h12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.minute:02d}:{dt.second:02d} {ampm} {dt.tzname()}"


def _quote(key, name, price, prev, ts, tz_name, is_open, source) -> dict:
    if price <= 0 or prev <= 0:
        raise ValueError("invalid price")
    chg = price - prev
    return {
        "key": key, "name": name,
        "price": round(price, 2),
        "change": round(chg, 2),
        "pct": round(chg / prev * 100, 2),
        "ts": int(ts),
        "time": fmt_time(int(ts), tz_name),
        "state": "LAST" if is_open else "CLOSE",
        "source": source,
        "stale": False,
    }


def parse_yahoo(payload: dict, key: str, name: str, tz_name: str, now: float) -> dict:
    meta = payload["chart"]["result"][0]["meta"]
    price = _num(meta["regularMarketPrice"])
    prev = _num(meta.get("previousClose") or meta.get("chartPreviousClose"))
    ts = int(meta.get("regularMarketTime") or now)
    reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    is_open = bool(reg) and int(reg.get("start", 0)) <= now < int(reg.get("end", 0))
    return _quote(key, name, price, prev, ts, tz_name, is_open, "yahoo")


def parse_history(payload: dict) -> dict:
    """Yahoo 6개월 일봉 → {"t": [epoch...], "c": [close...]} (결측치 제거)."""
    res = payload["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    t, c = [], []
    for x, y in zip(ts, closes):
        if isinstance(y, (int, float)) and y > 0:
            t.append(int(x))
            c.append(round(float(y), 4))
    if len(c) < 2:
        raise ValueError("history too short")
    return {"t": t, "c": c}


def parse_naver(payload: dict, key: str, name: str, tz_name: str, now: float) -> dict:
    price = _num(payload["closePrice"])
    chg = _num(payload["compareToPreviousClosePrice"])
    # 하락 시 부호가 없는 응답 대비 — 등락 코드(4·5 = 하락)로 부호 보정
    code = str((payload.get("compareToPreviousPrice") or {}).get("code", ""))
    if code in ("4", "5") and chg > 0:
        chg = -chg
    traded = payload.get("localTradedAt") or ""
    try:
        ts = datetime.fromisoformat(traded).timestamp()
    except ValueError:
        ts = now
    is_open = str(payload.get("marketStatus", "")).upper() == "OPEN"
    return _quote(key, name, price, price - chg, ts, tz_name, is_open, "naver")


class MarketService:
    def __init__(self, cfg):
        self.interval = cfg.get("MARKET_POLL_INTERVAL", 15)
        self.timeout = min(cfg.get("HTTP_TIMEOUT", 8), 6)
        self.s = requests.Session()
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=0)
        self.s.mount("https://", adapter)
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
        })
        self.pool = ThreadPoolExecutor(max_workers=len(INDICES), thread_name_prefix="mkt")
        self._snapshot: list[dict] = []
        self._updated = 0.0
        self._history: dict[str, dict] = {}
        self._history_at = 0.0
        self._last_access = 0.0
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────
    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="market-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.s.close()

    # ── public ───────────────────────────────────────────
    def snapshot(self) -> dict:
        now = time.time()
        idle = now - self._last_access > ACTIVE_SECONDS
        self._last_access = now
        if idle or not self._snapshot:
            self._wake.set()  # 쉬고 있었다면 즉시 수집
        return {"updated": int(self._updated), "interval": self.interval,
                "items": self._snapshot}

    def history(self) -> dict:
        self.snapshot()  # 조회 활동으로 기록(수집 시작 트리거)
        return {"updated": int(self._history_at), "items": self._history}

    # ── internals ────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.time() - self._last_access <= ACTIVE_SECONDS:
                try:
                    self.refresh()
                except RuntimeError:  # 종료 중 풀이 닫힌 경우
                    return
                except Exception:
                    log.exception("시세 수집 루프 오류")
                wait = self.interval if self._snapshot else 5
            else:
                wait = 3600  # 조회가 없으면 다음 조회(wake)까지 대기
            self._wake.wait(timeout=wait)
            self._wake.clear()

    def refresh(self) -> None:
        prev = {q["key"]: q for q in self._snapshot}
        results = list(self.pool.map(self._fetch_one, INDICES))
        snap = []
        for spec, q in zip(INDICES, results):
            if q is None:
                old = prev.get(spec[0])
                if old is None:
                    continue
                q = {**old, "stale": True}
            snap.append(q)
        if snap:
            self._snapshot = snap  # 참조 교체(원자적)
            self._updated = time.time()
        self._refresh_history()

    def _refresh_history(self) -> None:
        now = time.time()
        missing = len(self._history) < len(INDICES)
        # 빠진 종목이 있으면 5분 뒤 재시도, 모두 있으면 30분 주기
        if now - self._history_at < (300 if missing else HISTORY_TTL):
            return
        self._history_at = now
        results = list(self.pool.map(self._fetch_history, INDICES))
        hist = dict(self._history)
        for spec, h in zip(INDICES, results):
            if h is not None:
                hist[spec[0]] = h
        self._history = hist

    def _fetch_history(self, spec) -> dict | None:
        ysym = spec[2]
        for host in _YAHOO_HOSTS:
            try:
                r = self.s.get(f"https://{host}/v8/finance/chart/{ysym}",
                               params={"range": "6mo", "interval": "1d"},
                               timeout=self.timeout)
                r.raise_for_status()
                return parse_history(r.json())
            except Exception as e:
                log.debug("6개월 추이 실패[%s@%s]: %s", spec[0], host, type(e).__name__)
        log.warning("6개월 추이 수집 실패[%s]", spec[0])
        return None

    def _fetch_one(self, spec) -> dict | None:
        key, name, ysym, ncode, nkind, tz_name = spec
        now = time.time()
        for host in _YAHOO_HOSTS:
            try:
                r = self.s.get(f"https://{host}/v8/finance/chart/{ysym}",
                               params={"range": "1d", "interval": "1d"},
                               timeout=self.timeout)
                r.raise_for_status()
                return parse_yahoo(r.json(), key, name, tz_name, now)
            except Exception as e:
                log.debug("Yahoo 시세 실패[%s@%s]: %s", key, host, type(e).__name__)
        if not ncode:
            log.warning("시세 수집 실패[%s]", key)
            return None
        try:
            url = (f"https://m.stock.naver.com/api/index/{ncode}/basic" if nkind == "domestic"
                   else f"https://api.stock.naver.com/index/{ncode}/basic")
            r = self.s.get(url, timeout=self.timeout)
            r.raise_for_status()
            return parse_naver(r.json(), key, name, tz_name, now)
        except Exception as e:
            log.warning("시세 수집 실패[%s]: %s", key, type(e).__name__)
        return None
