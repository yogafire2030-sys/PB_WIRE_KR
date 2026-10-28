"""다음 N거래일(기본 5일 = 1주) 방향 전망 + 정직한 자기 채점.

설계 원칙
- 종속변수: t일 종가 → t+h일 종가 로그수익률 (h일 선행 누적수익률)
- 독립변수: t일 장 마감 시점에 '이미 알 수 있는' 값만 사용
  (추세·변동성·VIX 레벨·크레딧·금리·달러·원자재의 5~60일 변화 등 '느린' 신호)
- t일 학습에는 y가 확정된 표본(i + h ≤ t)만 사용 → 미래정보 완전 차단
- 겹치는 h일 수익률이라 Newey-West 시차를 2h로 크게 잡고, BH 다중검정 보정
- 상승확률 = Φ(예상수익률 / 잔차표준편차)
- 채점: 서로 겹치지 않는 주 단위 표본으로 '항상 다수 방향' 대비 이항검정
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

import config
from core import model
from core.data import DataError
from core.factory import _aligned, completed_cutoff, factor_level

FEATURE_NAMES = {
    "T_MOM5": "지수 1주 수익률", "T_MOM20": "지수 1개월 수익률", "T_MOM60": "지수 3개월 수익률",
    "T_MA200": "200일선 대비 이격", "T_VOL20": "지수 20일 변동성", "T_DD52": "52주 고점 대비 낙폭",
    "VIX_LVL": "VIX 레벨", "VIX_CHG5": "VIX 1주 변화",
    "HY_LVL": "하이일드 스프레드 레벨", "HY_CHG20": "하이일드 스프레드 1개월 변화",
    "HYG_RET20": "HYG 1개월 수익률",
    "UST10Y_CHG20": "미 10년물 1개월 변화", "CURVE": "장단기 금리차(10Y-2Y)",
    "DXY_RET20": "달러인덱스 1개월 변화", "USDKRW_RET20": "원/달러 1개월 변화",
    "SOX_RET20": "반도체지수 1개월 수익률", "COPPER_RET20": "구리 1개월 수익률",
    "WTI_RET20": "유가 1개월 수익률", "SPX_RET5": "S&P500 1주 수익률",
    "VIXTERM_LVL": "VIX 기간구조(1M−3M)", "MOVE_LVL": "MOVE(채권 변동성) 레벨",
    "AUDJPY_RET20": "호주달러/엔 1개월 변화", "CUAU_RET20": "구리/금 비율 1개월 변화",
    "EWY_RET5": "한국 ETF(EWY) 1주 수익률",
}


def build_features(raw: dict, target_key: str, h: int) -> tuple[pd.Series, pd.DataFrame]:
    t = config.TARGETS[target_key]
    yahoo, fred = raw["yahoo"], raw["fred"]
    if t["ticker"] not in yahoo.columns:
        raise DataError(f"{t['name']} 시세가 없습니다.")
    lvl = yahoo[t["ticker"]].dropna()
    lvl = lvl[lvl > 0]
    if raw.get("source") != "demo":
        lvl = lvl[lvl.index <= completed_cutoff(t["region"])]
    dates = pd.DatetimeIndex(lvl.index)
    t_order = config.CLOSE_ORDER[t["region"]]
    logp = np.log(lvl)
    r1 = logp.diff()

    def level(key):
        f = config.active_factors().get(key)
        if not f or key in t["exclude"]:
            return None
        src = factor_level(raw, f)
        if src is None:
            return None
        if raw.get("source") != "demo":
            src = src[src.index <= completed_cutoff(f["region"])]
        a = _aligned(src, dates, config.CLOSE_ORDER[f["region"]] > t_order)
        return a if a.notna().any() else None

    def ret(key, n):
        a = level(key)
        return None if a is None else np.log(a.where(a > 0)).diff(n)

    def chg(key, n):
        a = level(key)
        return None if a is None else a.diff(n)

    F: dict[str, pd.Series | None] = {
        "T_MOM5": logp.diff(5), "T_MOM20": logp.diff(20), "T_MOM60": logp.diff(60),
        "T_MA200": logp - np.log(lvl.rolling(200).mean()),
        "T_VOL20": np.log(r1.rolling(20).std()),
        "T_DD52": logp - np.log(lvl.rolling(252).max()),
        "VIX_LVL": level("VIX"), "VIX_CHG5": chg("VIX", 5),
        "UST10Y_CHG20": chg("UST10Y", 20),
        "DXY_RET20": ret("DXY", 20), "SOX_RET20": ret("SOX", 20),
        "COPPER_RET20": ret("COPPER", 20), "WTI_RET20": ret("WTI", 20),
    }
    hy = level("HYSPRD")
    if hy is not None:
        F["HY_LVL"], F["HY_CHG20"] = hy, hy.diff(20)
    else:
        F["HYG_RET20"] = ret("HYG", 20)
    curve = level("T10Y2Y")
    if curve is None:
        l10, l2 = level("UST10Y"), level("UST2YF")
        curve = (l10 - l2) if (l10 is not None and l2 is not None) else None
    F["CURVE"] = curve
    # 신규(2026-10-28): 레짐·위험선호 '레벨' 신호
    F["VIXTERM_LVL"] = level("VIXTERM")            # 1M−3M > 0 이면 패닉(역전)
    F["MOVE_LVL"] = level("MOVE")
    F["AUDJPY_RET20"] = ret("AUDJPY", 20)
    cu, au = level("COPPER"), level("GOLD")        # 구리/금: 주간 피처엔 금 단독 피처가 없어 선형결합 아님
    if cu is not None and au is not None:
        F["CUAU_RET20"] = np.log((cu / au).where((cu > 0) & (au > 0))).diff(20)
    if t["region"] == "ASIA":
        F["USDKRW_RET20"] = ret("USDKRW", 20)
        F["SPX_RET5"] = ret("SPX", 5)
        F["EWY_RET5"] = ret("EWY", 5)

    X = pd.DataFrame({k: v for k, v in F.items() if v is not None}, index=dates)
    X = X.replace([np.inf, -np.inf], np.nan)
    y_fwd = r1.rolling(h).sum().shift(-h)  # t+1 … t+h 누적 로그수익률

    # 최근 구간 결측이 많은 피처 제외(예: FRED 하이일드는 최근 3년만 제공)
    recent = X.iloc[-800:]
    keep = [c for c in X.columns if recent[c].notna().mean() >= 0.95]
    X = X[keep].dropna()
    return y_fwd.reindex(X.index), X


def run(y_fwd: pd.Series, X: pd.DataFrame, s: model.Settings) -> dict:
    h, W = s.horizon, s.fc_window
    Y = y_fwd.to_numpy(np.float64)
    A = X.to_numpy(np.float64)
    names = list(X.columns)
    dates = X.index
    T, m = A.shape
    first = h + W - 1                     # 첫 예측 가능한 날
    if T - first < 60:
        W = max(252, T - h - 300)          # 데이터가 짧으면 학습 창을 줄여서라도 계산
        first = h + W - 1
        if T - first < 60:
            raise DataError("전망 모델을 학습할 데이터가 부족합니다.")
    start = max(first, T - s.history)
    sel = model.Settings(alpha=s.alpha, correction=s.correction, corr_max=s.corr_max,
                         nw_lags=min(2 * h, 40))

    rec = []
    last_detail = None
    for e in range(start, T):
        lo, hi = e - h - W + 1, e - h + 1     # y가 확정된 학습 표본만
        Xw, yw = A[lo:hi], Y[lo:hi]
        mu, sd = Xw.mean(axis=0), Xw.std(axis=0, ddof=1)
        my, sy = float(yw.mean()), float(yw.std(ddof=1))
        ok = sd > 1e-12
        Z = np.zeros_like(Xw)
        Z[:, ok] = (Xw[:, ok] - mu[ok]) / sd[ok]
        cand = np.nonzero(ok)[0] if sy > 0 else np.array([], int)
        yz = (yw - my) / sy if sy > 0 else np.zeros_like(yw)
        status, t_last, p_last, res = model._select(Z, yz, cand, sel)
        active = res["active"]
        valid = active.size > 0 and res["Fp"] <= sel.alpha

        z_e = np.zeros(m)
        z_e[ok] = (A[e, ok] - mu[ok]) / sd[ok]
        contrib = np.zeros(m)
        if valid:
            contrib[active] = sy * res["beta"] * z_e[active]
            resid_sd = sy * math.sqrt(max(1.0 - res["r2"], 1e-6) * (len(yw) - 1) / max(len(yw) - active.size - 1, 1))
        else:
            resid_sd = sy
        pred = my + float(contrib.sum())
        prob = float(stats.norm.cdf(pred / resid_sd)) if resid_sd > 0 else 0.5
        rec.append((dates[e].strftime("%Y-%m-%d"), pred * 100, my * 100, prob,
                    Y[e] * 100 if np.isfinite(Y[e]) else np.nan, int(active.size),
                    res["adj"] if valid else np.nan))
        if e == T - 1:
            last_detail = {
                "resid_sd": resid_sd * 100, "base": my * 100, "valid": bool(valid),
                "window": W, "adj_r2": float(res["adj"]) if valid else None,
                "drivers": sorted(
                    [{"key": names[j], "name": FEATURE_NAMES.get(names[j], names[j]),
                      "contrib": float(contrib[j] * 100), "t": float(t_last[j]),
                      "z": float(z_e[j])} for j in active] if valid else [],
                    key=lambda d: -abs(d["contrib"])),
                "tested": len(cand),
            }

    df = pd.DataFrame(rec, columns=["date", "pred", "base", "prob", "real", "k", "adj_r2"])
    return {"records": df, "today": last_detail, "features": names}


def scorecard(df: pd.DataFrame, h: int) -> dict:
    """서로 겹치지 않는 h일 표본으로 채점."""
    done = df.dropna(subset=["real"])
    wk = done.iloc[::-1].iloc[::h].iloc[::-1]  # 최신 기준 h일 간격
    n = len(wk)
    if n < 15:
        return {"n": n}
    real, pred, prob, base = wk["real"].to_numpy(), wk["pred"].to_numpy(), wk["prob"].to_numpy(), wk["base"].to_numpy()
    up_rate = float((real > 0).mean())
    call_up = prob >= 0.5
    hit = float((call_up == (real > 0)).mean())
    naive = max(up_rate, 1 - up_rate)
    hits = int(round(hit * n))
    p_vs_naive = float(stats.binomtest(hits, n, min(naive, 0.999), alternative="greater").pvalue)
    sse, sst = float(((real - pred) ** 2).sum()), float(((real - base) ** 2).sum())

    def bucket(lo, hi):
        mk = (prob >= lo) & (prob < hi)
        return {"n": int(mk.sum()), "up": float((real[mk] > 0).mean()) if mk.any() else None}

    strat = float(np.exp(real[call_up].sum() / 100) - 1) * 100
    hold = float(np.exp(real.sum() / 100) - 1) * 100
    return {
        "n": n, "up_rate": up_rate, "hit": hit, "naive": naive, "p": p_vs_naive,
        "oos_r2": 1 - sse / sst if sst > 0 else None,
        "said_up": float(call_up.mean()),
        "buckets": {"down": bucket(0, 0.45), "flat": bucket(0.45, 0.55), "up": bucket(0.55, 1.01)},
        "strat": strat, "hold": hold,
    }


def verdict(sc: dict, today: dict, prob: float, target_name: str, h: int) -> dict:
    lines = []
    if sc.get("n", 0) < 15:
        return {"grade": "판단 보류", "lines": ["채점할 과거 표본이 부족합니다."]}
    if sc["p"] < 0.05 and (sc["oos_r2"] or 0) > 0:
        grade = "참고 가치 있음"
        lines.append(f"과거 {sc['n']}주 동안 적중률 {sc['hit']*100:.0f}%로, ‘항상 {('상승' if sc['up_rate']>=0.5 else '하락')}’ "
                     f"찍기({sc['naive']*100:.0f}%)보다 통계적으로 유의하게 나았습니다(p={sc['p']:.2f}).")
    elif sc["hit"] > sc["naive"]:
        grade = "약한 신호"
        lines.append(f"과거 {sc['n']}주 적중률 {sc['hit']*100:.0f}%로 ‘항상 {('상승' if sc['up_rate']>=0.5 else '하락')}’ "
                     f"찍기({sc['naive']*100:.0f}%)보다 조금 높지만, 우연과 구분되지 않습니다(p={sc['p']:.2f}).")
    else:
        grade = "참고용(우위 없음)"
        lines.append(f"과거 {sc['n']}주 적중률 {sc['hit']*100:.0f}%로, 단순히 ‘항상 "
                     f"{('상승' if sc['up_rate']>=0.5 else '하락')}’이라고 찍는 것({sc['naive']*100:.0f}%)보다 낫지 않았습니다. "
                     "이 숫자는 방향 예측보다 현재 위험 환경을 요약한 값으로 보는 게 맞습니다.")
    if sc["said_up"] > 0.9 or sc["said_up"] < 0.1:
        lines.append(f"모델이 과거 {sc['said_up']*100:.0f}%의 주에서 ‘상승’이라고 답했습니다. "
                     "사실상 평균적인 상승 추세(드리프트)를 반영한 것에 가깝습니다.")
    b = sc["buckets"]
    if b["up"]["n"] >= 5 and b["down"]["n"] >= 5:
        lines.append(f"확률 55% 이상이라고 했던 주는 실제로 {b['up']['up']*100:.0f}%({b['up']['n']}주)가 올랐고, "
                     f"45% 미만이라고 했던 주는 {b['down']['up']*100:.0f}%({b['down']['n']}주)가 올랐습니다.")
    lines.append(f"‘확률 50% 이상일 때만 보유’ 전략의 누적수익률은 {sc['strat']:+.1f}%, 계속 보유는 {sc['hold']:+.1f}%였습니다. "
                 "거래비용은 반영하지 않았습니다.")
    if 0.45 <= prob <= 0.55:
        lines.append("이번 주 확률은 45~55% 사이라 방향성 신호가 사실상 중립입니다.")
    if today and not today["valid"]:
        lines.append("오늘은 유의한 선행 팩터가 없어서, 과거 평균 수익률만으로 확률을 냈습니다.")
    lines.append("선거·FOMC·실적 같은 일정 이벤트는 모델에 들어 있지 않습니다.")
    return {"grade": grade, "lines": lines}
