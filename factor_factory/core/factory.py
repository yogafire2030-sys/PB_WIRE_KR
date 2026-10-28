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


KST = "Asia/Seoul"


def completed_cutoff(region: str, now: pd.Timestamp | None = None) -> pd.Timestamp:
    """해당 시장에서 '장 마감이 끝난' 마지막 날짜(KST 기준 판단).
    ASIA: 15:40 KST 이후면 오늘까지 / US·FX·CRYPTO: 다음날 06:10 KST 이후에 그날 완료."""
    now = now or pd.Timestamp.now(tz=KST)
    if region == "ASIA":
        return (now - pd.Timedelta(hours=15, minutes=40)).tz_localize(None).normalize()
    return (now - pd.Timedelta(hours=6, minutes=10)).tz_localize(None).normalize() - pd.Timedelta(days=1)


def _strict(mode: str, f_region: str, t_region: str) -> bool:
    """True면 대상 지수 날짜 '이전' 값만 사용."""
    if mode == "preopen":
        # 대상 지수 개장 전에 이미 마감된 시장만 같은 날짜 허용: 아시아장(당일) → 미국 지수
        return not (f_region == "ASIA" and t_region == "US")
    return config.CLOSE_ORDER[f_region] > config.CLOSE_ORDER[t_region]


def build(raw: dict, target_key: str, mode: str):
    """(y: 일간 로그수익률, X: 후보 팩터, x_next: 다음 세션 팩터값 또는 None, next_date)

    mode
      explain : 같은 날 정보로 설명 (동행 분석)
      predict : 모든 시장 팩터를 하루 더 늦춤 (보수적 예측)
      preopen : 대상 지수 개장 전에 이미 마감된 정보만 사용 (개장 전 예측)
    """
    t = config.TARGETS[target_key]
    yahoo, fred = raw["yahoo"], raw["fred"]
    if t["ticker"] not in yahoo.columns:
        raise DataError(f"{t['name']} 시세가 없습니다.")
    lvl = yahoo[t["ticker"]].dropna()
    lvl = lvl[lvl > 0]
    if raw.get("source") != "demo":  # 장중 미완성 봉 제거
        lvl = lvl[lvl.index <= completed_cutoff(t["region"])]
    if len(lvl) < 300:
        raise DataError(f"{t['name']} 시세 데이터가 부족합니다({len(lvl)}일).")

    dates = pd.DatetimeIndex(lvl.index)
    next_date = dates[-1] + pd.offsets.BDay(1)
    ext = dates.append(pd.DatetimeIndex([next_date]))
    y_ext = pd.concat([np.log(lvl).diff(), pd.Series([np.nan], index=[next_date])])

    cols: dict[str, pd.Series] = {}
    next_ok = mode != "explain"
    for key, f in config.FACTORS.items():
        if key in t["exclude"] or f["source"] == "tech":
            continue
        frame = yahoo if f["source"] == "yahoo" else fred
        if frame is None or f["id"] not in frame.columns:
            continue
        strict = _strict(mode, f["region"], t["region"])
        src = frame[f["id"]]
        if raw.get("source") != "demo":
            src = src[src.index <= completed_cutoff(f["region"])]
        a = _aligned(src, ext, strict)
        s = np.log(a.where(a > 0)).diff() if f["transform"] == "logret" else a.diff()
        if mode == "predict":
            s = s.shift(1)
        # 다음 세션 계산에 필요한 그 시장의 최신 거래일이 아직 마감 전이면 계산 불가
        # (strict: 직전 날짜까지 필요 / 같은 날짜 허용: 다음 세션 날짜까지 필요)
        if mode == "preopen":
            need = dates[-1] if strict else next_date
            if raw.get("source") != "demo" and completed_cutoff(f["region"]) < need:
                next_ok = False
            if not strict and (src.dropna().index.max() < next_date):  # 그날 값이 아직 없음
                next_ok = False
        cols[key] = s
    cols.update(_tech(y_ext))

    X = pd.DataFrame(cols, index=ext)
    hist = X.iloc[:-1]
    recent = hist.iloc[-600:]
    keep = [c for c in X.columns if recent[c].notna().mean() >= 0.9]
    # 대체 팩터는 원래 팩터를 쓸 수 없을 때만 남긴다
    keep = [c for c in keep
            if config.FACTORS.get(c, {}).get("fallback_for") not in keep]
    df = pd.concat([y_ext.iloc[:-1].rename("__y__"), hist[keep]], axis=1)
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 300:
        raise DataError("정렬 후 유효 데이터가 부족합니다.")
    x_next = X[keep].iloc[-1].replace([np.inf, -np.inf], np.nan)
    if not next_ok or x_next.isna().any():
        x_next = None
    return df["__y__"], df[keep], x_next, next_date
