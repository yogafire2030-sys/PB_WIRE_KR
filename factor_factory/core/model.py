"""롤링 멀티팩터 회귀 엔진.

매 거래일 t마다 [t-W+1, t] 구간으로:
  1) 표준화(z-score) → 계수는 '표준화 베타'로 팩터 간 비교 가능
  2) 상관 제거: 종속변수와 상관이 큰 순서로, 이미 채택된 팩터와 |ρ| ≥ corr_max 이면 제외
  3) 후진제거: Newey-West(HAC) 표준오차 기반 t-검정, 기준 미달 팩터를 하나씩 제거
     - none : p ≤ α
     - bh   : Benjamini-Hochberg FDR 보정 (다중검정 보정)
     - t3   : p ≤ α 이고 |t| ≥ 3 (Harvey-Liu-Zhu 기준)
  4) F-검정(HAC Wald): 남은 팩터 전체가 유의해야 그날 모델을 '유효'로 인정
  5) 가중치: 당일 |t| 비중 → EWMA(반감기 h) 평활 → 하루 결과로 널뛰지 않음
  6) 표본외(OOS) 검증: t-1일까지의 평활 모델로 t일 수익률을 계산해 실제와 비교
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.special import fdtrc, stdtr  # 분포 객체보다 수십 배 빠른 ufunc

ST_SEL, ST_INS, ST_PRN, ST_NOV = "S", "I", "P", "N"  # 채택 / 비유의 / 상관제거 / 분산없음


@dataclass(frozen=True)
class Settings:
    window: int = 252        # 롤링 창(거래일)
    alpha: float = 0.05      # 유의수준
    mode: str = "predict"    # predict(전일 팩터로 예측, 기본) | explain(동시점 설명)
    correction: str = "bh"   # none | bh | t3
    corr_max: float = 0.80   # 다중공선성 제거 임계치
    halflife: int = 10       # 가중치 EWMA 반감기(거래일)
    nw_lags: int = 5         # Newey-West 시차
    history: int = 500       # 계산·저장할 최근 거래일 수
    horizon: int = 5         # 전망 기간(거래일) — 5 = 1주
    fc_window: int = 756     # 전망 모델 학습 창(거래일) — 3년

    LIMITS = {
        "window": (60, 1000), "alpha": (0.001, 0.2), "corr_max": (0.3, 0.99),
        "halflife": (1, 120), "nw_lags": (0, 20), "history": (60, 2500),
        "horizon": (1, 20), "fc_window": (252, 1500),
    }
    CHOICES = {"mode": ("explain", "predict"), "correction": ("none", "bh", "t3")}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        """외부 입력 검증: 화이트리스트 + 범위 클램프. 잘못된 값은 기본값."""
        base = asdict(cls())
        if not isinstance(d, dict):
            return cls()
        for k, default in base.items():
            if k not in d:
                continue
            v = d[k]
            if k in cls.CHOICES:
                base[k] = v if v in cls.CHOICES[k] else default
                continue
            try:
                v = type(default)(float(v))
            except (TypeError, ValueError):
                continue
            if not np.isfinite(v):
                continue
            lo, hi = cls.LIMITS[k]
            base[k] = min(max(v, type(default)(lo)), type(default)(hi))
        return cls(**base)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 통계 유틸
# ---------------------------------------------------------------------------
def _inv(G: np.ndarray) -> np.ndarray:
    try:
        return np.linalg.inv(G)
    except np.linalg.LinAlgError:  # 특이행렬 대비
        return np.linalg.pinv(G)


def _ols_hac(X: np.ndarray, y: np.ndarray, G: np.ndarray, Xy: np.ndarray, lags: int):
    """중심화된 X, y에 대한 OLS + Newey-West 공분산. (beta, t, cov, resid, df)
    G = X'X, Xy = X'y 는 창 단위로 한 번만 계산해 부분집합만 잘라 쓴다(후진제거 가속)."""
    n, k = X.shape
    df = max(n - k - 1, 1)
    xtx_inv = _inv(G)
    beta = xtx_inv @ Xy
    u = y - X @ beta
    if lags > 0:
        xu = X * u[:, None]
        S = xu.T @ xu
        for l in range(1, min(lags, n - 1) + 1):
            g = xu[l:].T @ xu[:-l]
            S += (1.0 - l / (lags + 1.0)) * (g + g.T)
        cov = xtx_inv @ S @ xtx_inv * (n / df)
    else:
        cov = xtx_inv * (u @ u / df)
    se = np.sqrt(np.clip(np.diag(cov), 1e-300, None))
    return beta, beta / se, cov, u, df


def _failing(p: np.ndarray, t: np.ndarray, s: Settings, m_tests: int) -> np.ndarray:
    if s.correction == "bh":
        order = np.argsort(p)
        thresh = s.alpha * np.arange(1, len(p) + 1) / max(m_tests, 1)
        ok_sorted = p[order] <= thresh
        k = np.nonzero(ok_sorted)[0]
        passed = np.zeros(len(p), bool)
        if k.size:
            passed[order[: k[-1] + 1]] = True
        return ~passed
    fail = p > s.alpha
    if s.correction == "t3":
        fail |= np.abs(t) < 3.0
    return fail


def _select(Z: np.ndarray, yz: np.ndarray, cand: np.ndarray, s: Settings):
    n = Z.shape[0]
    m = Z.shape[1]
    status = np.full(m, ST_NOV, dtype="<U1")
    t_last = np.full(m, np.nan)
    p_last = np.full(m, np.nan)
    out = {"active": np.array([], int), "beta": None, "t": None, "p": None,
           "F": np.nan, "Fp": np.nan, "r2": np.nan, "adj": np.nan}
    if cand.size == 0:
        return status, t_last, p_last, out

    G_all = Z.T @ Z          # 창 전체 Gram 행렬 1회 계산
    Zy_all = Z.T @ yz
    corr_y = Zy_all[cand] / (n - 1)
    C = G_all[np.ix_(cand, cand)] / (n - 1)
    kept: list[int] = []
    for j in np.argsort(-np.abs(corr_y)):
        if all(abs(C[j, i]) < s.corr_max for i in kept):
            kept.append(j)
        else:
            status[cand[j]] = ST_PRN
    active = cand[np.array(kept, int)]
    m_tests = active.size

    while active.size:
        beta, t, cov, u, df = _ols_hac(Z[:, active], yz, G_all[np.ix_(active, active)],
                                       Zy_all[active], s.nw_lags)
        p = 2.0 * stdtr(df, -np.abs(t))
        t_last[active], p_last[active] = t, p
        fail = _failing(p, t, s, m_tests)
        if not fail.any():
            k = active.size
            try:
                F = float(beta @ np.linalg.solve(cov, beta)) / k
            except np.linalg.LinAlgError:
                F = float(beta @ np.linalg.pinv(cov) @ beta) / k
            sst = float(yz @ yz)
            r2 = 1.0 - float(u @ u) / sst if sst > 0 else np.nan
            out.update(active=active, beta=beta, t=t, p=p, F=F,
                       Fp=float(fdtrc(k, df, F)), r2=r2,
                       adj=1.0 - (1.0 - r2) * (n - 1) / df)
            status[active] = ST_SEL
            break
        worst = int(np.argmax(np.where(fail, p, -1.0)))
        status[active[worst]] = ST_INS
        active = np.delete(active, worst)
    return status, t_last, p_last, out


# ---------------------------------------------------------------------------
# 메인 루프
# ---------------------------------------------------------------------------
def run(y: pd.Series, X: pd.DataFrame, s: Settings) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(daily_stats, factor_daily) 반환. 수익률 관련 값은 % 단위."""
    Y = y.to_numpy(np.float64)
    A = X.to_numpy(np.float64)
    names = list(X.columns)
    dates = y.index
    T, m = A.shape
    W = s.window
    if T < W + 30:
        raise ValueError(f"데이터({T}일)가 롤링 창({W}일)보다 충분히 길지 않습니다.")

    record_from = max(W - 1, T - s.history)
    warmup = min(4 * s.halflife, record_from - (W - 1))
    lam = 0.5 ** (1.0 / s.halflife)

    B = np.zeros(m)      # 평활 표준화 베타(부호 포함)
    Wt = np.zeros(m)     # 평활 중요도 가중치
    prev = None
    stat_rows, fac_rows = [], []

    for e in range(record_from - warmup, T):
        Xw = A[e - W + 1: e + 1]
        yw = Y[e - W + 1: e + 1]

        # (6) 표본외: 어제까지의 모델로 오늘 계산
        if prev is not None:
            pmu, psd, pmy, psy, pB = prev
            z = np.divide(A[e] - pmu, psd, out=np.zeros(m), where=psd > 0)
            pred, bench = pmy + psy * float(pB @ z), pmy
        else:
            pred = bench = np.nan

        mu = Xw.mean(axis=0)
        sd = Xw.std(axis=0, ddof=1)
        my, sy = float(yw.mean()), float(yw.std(ddof=1))
        ok = sd > 1e-12
        Z = np.zeros_like(Xw)
        Z[:, ok] = (Xw[:, ok] - mu[ok]) / sd[ok]
        cand = np.nonzero(ok)[0] if sy > 0 else np.array([], int)
        yz = (yw - my) / sy if sy > 0 else np.zeros_like(yw)

        status, t_last, p_last, res = _select(Z, yz, cand, s)
        active = res["active"]
        valid = active.size > 0 and res["Fp"] <= s.alpha

        beta_today = np.zeros(m)
        w_raw = np.zeros(m)
        contrib = np.zeros(m)  # 당일 수익률 분해(%p): σy·β·z
        if valid:
            beta_today[active] = res["beta"]
            at = np.abs(res["t"])
            w_raw[active] = at / at.sum()
            contrib[active] = sy * res["beta"] * Z[-1, active] * 100.0
        B = lam * B + (1.0 - lam) * beta_today
        Wt = lam * Wt + (1.0 - lam) * w_raw
        prev = (mu, sd, my, sy, B.copy())

        if e < record_from:
            continue
        d = dates[e].strftime("%Y-%m-%d")
        wsum = Wt.sum()
        w_norm = Wt / wsum if wsum > 0 else Wt
        stat_rows.append((d, W, int(active.size), res["r2"], res["adj"], res["F"], res["Fp"],
                          int(valid), Y[e] * 100.0, pred * 100.0, bench * 100.0,
                          my * 100.0 + float(contrib.sum()) if valid else np.nan))
        beta_full = np.full(m, np.nan)
        if active.size:
            beta_full[active] = res["beta"]
        for j in range(m):
            fac_rows.append((d, names[j], beta_full[j], t_last[j], p_last[j], status[j],
                             w_raw[j], w_norm[j], B[j], contrib[j]))

    stats_df = pd.DataFrame(stat_rows, columns=[
        "date", "n", "k", "r2", "adj_r2", "f_stat", "f_p", "valid", "actual", "pred", "bench", "fit"])
    fac_df = pd.DataFrame(fac_rows, columns=[
        "date", "factor", "beta", "t", "p", "status", "w_raw", "w_ewma", "b_ewma", "contrib"])
    return stats_df, fac_df
