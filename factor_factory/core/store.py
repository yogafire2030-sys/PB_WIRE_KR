"""SQLite 저장소. 모든 연결은 with 블록으로 즉시 닫아 핸들 누수를 막는다."""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime

import pandas as pd

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    target TEXT PRIMARY KEY, settings TEXT NOT NULL, source TEXT NOT NULL,
    updated_at TEXT NOT NULL, last_date TEXT, factors TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_stats (
    target TEXT NOT NULL, date TEXT NOT NULL, n INTEGER, k INTEGER,
    r2 REAL, adj_r2 REAL, f_stat REAL, f_p REAL, valid INTEGER,
    actual REAL, pred REAL, bench REAL, fit REAL,
    PRIMARY KEY (target, date)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS factor_daily (
    target TEXT NOT NULL, date TEXT NOT NULL, factor TEXT NOT NULL,
    beta REAL, t REAL, p REAL, status TEXT, w_raw REAL, w_ewma REAL, b_ewma REAL, contrib REAL,
    PRIMARY KEY (target, date, factor)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS forecasts (
    target TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


_MIGRATIONS = {"daily_stats": {"fit": "REAL"}, "factor_daily": {"contrib": "REAL"}}


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        # 이전 버전 DB에 새 컬럼 추가 (식별자는 고정 화이트리스트에서만 사용)
        for table, cols in _MIGRATIONS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def save_run(target: str, settings: dict, source: str,
             stats_df: pd.DataFrame, fac_df: pd.DataFrame) -> None:
    """대상 지수 결과를 원자적으로 교체 저장."""
    st_rows = [(target, r.date, int(r.n), int(r.k), _num(r.r2), _num(r.adj_r2), _num(r.f_stat),
                _num(r.f_p), int(r.valid), _num(r.actual), _num(r.pred), _num(r.bench), _num(r.fit))
               for r in stats_df.itertuples(index=False)]
    fc_rows = [(target, r.date, r.factor, _num(r.beta), _num(r.t), _num(r.p), r.status,
                _num(r.w_raw), _num(r.w_ewma), _num(r.b_ewma), _num(r.contrib))
               for r in fac_df.itertuples(index=False)]
    last_date = stats_df["date"].iloc[-1] if len(stats_df) else None
    factors = json.dumps(sorted(fac_df["factor"].unique().tolist()))
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM daily_stats WHERE target=?", (target,))
        conn.execute("DELETE FROM factor_daily WHERE target=?", (target,))
        conn.executemany("INSERT INTO daily_stats (target, date, n, k, r2, adj_r2, f_stat, f_p, valid, "
                         "actual, pred, bench, fit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", st_rows)
        conn.executemany("INSERT INTO factor_daily (target, date, factor, beta, t, p, status, w_raw, "
                         "w_ewma, b_ewma, contrib) VALUES (?,?,?,?,?,?,?,?,?,?,?)", fc_rows)
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)",
            (target, json.dumps(settings), source,
             datetime.now().strftime("%Y-%m-%d %H:%M"), last_date, factors))


def load_run(target: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT settings, source, updated_at, last_date, factors FROM runs WHERE target=?",
            (target,)).fetchone()
    if not row:
        return None
    return {"settings": json.loads(row[0]), "source": row[1], "updated_at": row[2],
            "last_date": row[3], "factors": json.loads(row[4])}


def load_stats(target: str, days: int) -> pd.DataFrame:
    with closing(_connect()) as conn:
        df = pd.read_sql_query(
            "SELECT * FROM (SELECT * FROM daily_stats WHERE target=? ORDER BY date DESC LIMIT ?) "
            "ORDER BY date", conn, params=(target, days))
    return df


def load_factors(target: str, since: str) -> pd.DataFrame:
    with closing(_connect()) as conn:
        df = pd.read_sql_query(
            "SELECT date, factor, beta, t, p, status, w_raw, w_ewma, b_ewma, contrib FROM factor_daily "
            "WHERE target=? AND date>=? ORDER BY date", conn, params=(target, since))
    return df


def save_forecast(target: str, payload: dict) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO forecasts VALUES (?,?,?)",
                     (target, datetime.now().strftime("%Y-%m-%d %H:%M"),
                      json.dumps(payload, ensure_ascii=False, allow_nan=False)))


def load_forecast(target: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT payload FROM forecasts WHERE target=?", (target,)).fetchone()
    return json.loads(row[0]) if row else None
