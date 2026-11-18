"""Stocktwits 스냅샷 저장소 (SQLite).

요청마다 연결을 열고 반드시 닫아 커넥션 누수를 방지합니다.
모든 쿼리는 파라미터 바인딩을 사용합니다(SQL Injection 방지).
"""
import os
import sqlite3
import threading
import time
from contextlib import closing

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buzz_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    msgs_24h INTEGER NOT NULL,
    msgs_prev24h INTEGER NOT NULL,
    watchers INTEGER,
    bull INTEGER NOT NULL,
    bear INTEGER NOT NULL,
    saturated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_buzz_symbol_ts ON buzz_snapshot(symbol, ts);
"""
MIN_INTERVAL_SEC = 30 * 60        # 같은 종목은 30분에 1회만 저장
RETENTION_SEC = 365 * 24 * 3600   # 1년 보관
_write_lock = threading.Lock()
_db_path = None


def init(path: str) -> None:
    global _db_path
    _db_path = path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with closing(_connect()) as conn, conn:
        conn.executescript(_SCHEMA)
        conn.execute("DELETE FROM buzz_snapshot WHERE ts < ?", (int(time.time()) - RETENTION_SEC,))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path, timeout=10, isolation_level="DEFERRED")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def save_snapshots(results: list) -> None:
    now = int(time.time())
    rows = [r for r in results if r.get("ok") and not r.get("cached")]
    if not rows:
        return
    with _write_lock, closing(_connect()) as conn, conn:
        last = {}
        syms = [r["symbol"] for r in rows]
        q = f"SELECT symbol, MAX(ts) AS ts FROM buzz_snapshot WHERE symbol IN ({','.join('?' * len(syms))}) GROUP BY symbol"
        for rec in conn.execute(q, syms):
            last[rec["symbol"]] = rec["ts"]
        batch = [
            (r["symbol"], now, r["msgs_24h"], r["msgs_prev24h"], r["watchers"], r["bull"], r["bear"], int(r["saturated"]))
            for r in rows if now - last.get(r["symbol"], 0) >= MIN_INTERVAL_SEC
        ]
        if batch:
            conn.executemany(
                "INSERT INTO buzz_snapshot(symbol, ts, msgs_24h, msgs_prev24h, watchers, bull, bear, saturated) "
                "VALUES (?,?,?,?,?,?,?,?)", batch)


def history(symbol: str, limit: int = 120) -> list:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT ts, msgs_24h, watchers, bull, bear, saturated FROM buzz_snapshot "
            "WHERE symbol = ? ORDER BY ts DESC LIMIT ?", (symbol, int(limit)))
        data = [dict(r) for r in cur]
    data.reverse()
    return data


def watcher_deltas(symbols: list, days: int = 7) -> dict:
    """기간 내 첫 스냅샷 대비 관심자 수 증감."""
    if not symbols:
        return {}
    since = int(time.time()) - days * 86400
    ph = ",".join("?" * len(symbols))
    sql = (
        "SELECT s.symbol, s.watchers FROM buzz_snapshot s JOIN ("
        f"  SELECT symbol, MIN(ts) AS ts FROM buzz_snapshot WHERE symbol IN ({ph}) AND ts >= ? AND watchers IS NOT NULL GROUP BY symbol"
        ") f ON s.symbol = f.symbol AND s.ts = f.ts"
    )
    with closing(_connect()) as conn:
        return {r["symbol"]: r["watchers"] for r in conn.execute(sql, [*symbols, since])}
