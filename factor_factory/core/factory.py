"""팩터 팩토리: 원천 시계열 → 대상 지수 달력에 정렬된 후보 팩터 행렬.

핵심 원칙
1) 레벨을 먼저 대상 지수 날짜에 as-of 정렬한 뒤 변화율을 계산한다.
   → 휴장일 차이로 수익률이 누락되거나 중복 계산되지 않는다.
2) 대상 지수보다 늦게 마감하는 시장(예: KOSPI 대비 미국·환율)은
   '해당 날짜 이전' 값만 사용한다(allow_exact_matches=False).
3) 예측 모드에서는 모든 시장 팩터를 1거래일 더 늦춘다(x[t-1] → y[t]).
4) 기술적 팩터는 모드와 무관하게 전일까지의 정보만 사용한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config
from core.data import DataError

STALE_LIMIT = pd.Timedelta(days=7)  # 이보다 오래된 값은 결측 처리


def _aligned(level: pd.Series, dates: pd.DatetimeIndex, strict: bool) -> pd.Series:
    s = level.dropna()
    if s.empty:
        return pd.Series(np.nan, index=dates)
    left = pd.DataFrame({"date": dates.values.astype("datetime64[ns]")})
    right = pd.DataFrame({"date": s.index.values.astype("datetime64[ns]"), "v": s.to_numpy(float)})
    m = pd.merge_asof(left, right, on="date", direction="backward",
                      allow_exact_matches=not strict, tolerance=STALE_LIMIT)
    return pd.Series(m["v"].to_numpy(), index=dates)


def _tech(y: pd.Series) -> dict[str, pd.Series]:
    vol = y.rolling(20).std()
    return {
        "MOM5": y.rolling(5).sum().shift(1),
        "MOM20": y.rolling(20).sum().shift(1),
        "REV1": y.shift(1),
        "RVOL": np.log(vol.where(vol > 0)).diff().shift(1),
    }


def build(raw: dict, target_key: str, mode: str) -> tuple[pd.Series, pd.DataFrame]:
    """(y: 일간 로그수익률, X: 후보 팩터) — 결측 없는 공통 구간만 반환."""
    t = config.TARGETS[target_key]
    yahoo, fred = raw["yahoo"], raw["fred"]
    if t["ticker"] not in yahoo.columns:
        raise DataError(f"{t['name']} 시세가 없습니다.")
    lvl = yahoo[t["ticker"]].dropna()
    lvl = lvl[lvl > 0]
    if len(lvl) < 300:
        raise DataError(f"{t['name']} 시세 데이터가 부족합니다({len(lvl)}일).")

    dates = pd.DatetimeIndex(lvl.index)
    y = np.log(lvl).diff()
    t_order = config.CLOSE_ORDER[t["region"]]

    cols: dict[str, pd.Series] = {}
    for key, f in config.FACTORS.items():
        if key in t["exclude"] or f["source"] == "tech":
            continue
        frame = yahoo if f["source"] == "yahoo" else fred
        if frame is None or f["id"] not in frame.columns:
            continue
        strict = config.CLOSE_ORDER[f["region"]] > t_order
        a = _aligned(frame[f["id"]], dates, strict)
        if f["transform"] == "logret":
            s = np.log(a.where(a > 0)).diff()
        else:
            s = a.diff()
        if mode == "predict":
            s = s.shift(1)
        cols[key] = s
    cols.update(_tech(y))

    X = pd.DataFrame(cols, index=dates)
    recent = X.iloc[-600:]
    keep = [c for c in X.columns if recent[c].notna().mean() >= 0.9]
    # 대체 팩터는 원래 팩터를 쓸 수 없을 때만 남긴다
    keep = [c for c in keep
            if config.FACTORS.get(c, {}).get("fallback_for") not in keep]
    df = pd.concat([y.rename("__y__"), X[keep]], axis=1)
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 300:
        raise DataError("정렬 후 유효 데이터가 부족합니다.")
    return df["__y__"], df[keep]
