"""스크리닝 데이터 엔진 — 대상 종목 전체를 백그라운드에서 주기적으로 계산.

- 화면 요청은 메모리의 최신 결과(스냅샷)에서 바로 필터링 → 응답 즉시
- 결과가 오래되면(기본 60분) 다음 조회 때 백그라운드 재계산을 시작하고, 끝날 때까지 이전 결과 제공
- 조회가 없으면 계산하지 않음(Render 무료 플랜·Yahoo 호출 절약)
- 종목당 1회 Yahoo 1년 일봉 조회, 동시 6개, 429 시 잠시 쉬고 재시도
- 스냅샷에는 계산된 지표만 보관(가격 배열 미보관) → 메모리 일정
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import minervini, universe
from .http_client import get_session

log = logging.getLogger(__name__)

_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
UNIVERSE_TTL = 24 * 3600


class ScanEngine:
    def __init__(self, refresh_sec: int = 3600, workers: int = 6, timeout=(5, 15)):
        self.refresh_sec = refresh_sec
        self.workers = workers
        self.timeout = timeout
        self._lock = threading.Lock()
        self._running = False
        self._rows: dict[str, dict] = {}
        self._updated = 0.0
        self._done = self._total = self._failed = 0
        self._members: dict[str, frozenset] = {}
        self._source = ""
        self._universe_at = 0.0
        self._backoff_until = 0.0

    # ── public ───────────────────────────────────────────
    def ensure_fresh(self) -> None:
        if self._running or time.time() - self._updated < self.refresh_sec:
            return
        with self._lock:
            if self._running:
                return
            self._running = True
        threading.Thread(target=self._run, name="scan-engine", daemon=True).start()

    def status(self) -> dict:
        return {"ready": bool(self._rows), "running": self._running, "done": self._done,
                "total": self._total, "failed": self._failed, "count": len(self._rows),
                "updated": int(self._updated), "source": self._source,
                "refresh_min": self.refresh_sec // 60}

    def rows(self, universe_key: str) -> list:
        rows = self._rows  # 참조 1회 획득(교체 중에도 일관된 스냅샷)
        members = self._members.get(universe_key)
        if universe_key == "all" or members is None:
            return list(rows.values())
        return [r for t, r in rows.items() if t in members]

    def index_of(self, ticker: str) -> list:
        return [k for k, m in self._members.items() if ticker in m]

    # ── internals ────────────────────────────────────────
    def _run(self) -> None:
        try:
            session = get_session()
            if time.time() - self._universe_at > UNIVERSE_TTL or not self._members:
                u = universe.fetch(session, self.timeout)
                self._members = {"sp500": frozenset(u["sp500"]), "ndx": frozenset(u["ndx"])}
                self._source, self._universe_at = u["source"], time.time()
            tickers = sorted(self._members["sp500"] | self._members["ndx"])
            self._total, self._done, self._failed = len(tickers), 0, 0
            out = {}
            with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="scan") as pool:
                for t, row in zip(tickers, pool.map(self._fetch, tickers)):
                    self._done += 1
                    if row is None:
                        self._failed += 1
                    else:
                        out[t] = row
            if out:
                minervini.rank_rs(list(out.values()))
                self._rows, self._updated = out, time.time()  # 참조 교체(원자적)
            log.info("스크리닝 데이터 갱신: %d종목 (실패 %d)", len(out), self._failed)
        except Exception:
            log.exception("스크리닝 데이터 계산 실패")
        finally:
            self._running = False

    def _fetch(self, ticker: str):
        session = get_session()
        for attempt in range(3):  # 재시도는 429(요청 과다)일 때만
            wait = self._backoff_until - time.time()
            if wait > 0:
                time.sleep(min(wait, 30))
            limited = False
            for host in _YAHOO_HOSTS:
                try:
                    r = session.get(f"https://{host}/v8/finance/chart/{ticker}",
                                    params={"range": "1y", "interval": "1d"}, timeout=self.timeout)
                    try:
                        if r.status_code == 429:  # 전체 작업을 잠시 늦춤
                            self._backoff_until = time.time() + 5 * (attempt + 1)
                            limited = True
                            break
                        if r.status_code == 404:
                            return None
                        r.raise_for_status()
                        return minervini.compute(ticker, r.json())
                    finally:
                        r.close()
                except Exception as e:
                    log.debug("Yahoo 조회 실패[%s@%s]: %s", ticker, host, type(e).__name__)
            if not limited:
                return None
        return None
