"""대시보드용 데이터 가공 (DB → JSON 직렬화 가능한 dict)."""
from __future__ import annotations

import logging
import math

import numpy as np
import pandas as pd

import config
from core import insight, store

CHART_DAYS = 250
HEAT_DAYS = 60
OOS_DAYS = 60


def _f(v, nd=4):
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


def _list(series, nd=4):
    return [_f(v, nd) for v in series]


def _oos(st: pd.DataFrame) -> dict:
    d = st.dropna(subset=["pred", "actual", "bench"]).tail(OOS_DAYS)
    if len(d) < 10:
        return {"oos_r2": None, "hit": None, "corr": None, "n": len(d)}
    a, p, b = d["actual"].to_numpy(), d["pred"].to_numpy(), d["bench"].to_numpy()
    sse, sst = float(((a - p) ** 2).sum()), float(((a - b) ** 2).sum())
    nz = (a != 0) & (p != 0)
    hit = float((np.sign(a[nz]) == np.sign(p[nz])).mean()) if nz.any() else None
    corr = float(np.corrcoef(a, p)[0, 1]) if np.std(p) > 0 else None
    return {"oos_r2": _f(1 - sse / sst if sst > 0 else None),
            "hit": _f(hit), "corr": _f(corr), "n": len(d)}


def dashboard(target: str) -> dict | None:
    run = store.load_run(target)
    if not run:
        return None
    st = store.load_stats(target, CHART_DAYS)
    if st.empty:
        return None
    since = st["date"].iloc[0]
    fd = store.load_factors(target, since)

    dates = st["date"].tolist()
    last = st.iloc[-1]

    # 피벗: (날짜 × 팩터)
    piv = {c: fd.pivot(index="date", columns="factor", values=c).reindex(dates)
           for c in ("t", "w_ewma", "b_ewma")}
    status = fd.pivot(index="date", columns="factor", values="status").reindex(dates)

    factors = list(piv["w_ewma"].columns)
    heat_dates = dates[-HEAT_DAYS:]
    freq = (status.tail(HEAT_DAYS) == "S").mean()
    order = sorted(factors, key=lambda f: (-(piv["w_ewma"][f].iloc[-1] or 0), -freq.get(f, 0)))

    today = fd[fd["date"] == dates[-1]].set_index("factor")
    rows = []
    for f in order:
        meta = config.FACTORS.get(f, {})
        r = today.loc[f] if f in today.index else None
        rows.append({
            "key": f, "name": meta.get("name", f), "group": meta.get("group", ""),
            "status": r["status"] if r is not None else "N",
            "beta": _f(r["beta"]) if r is not None else None,
            "t": _f(r["t"], 2) if r is not None else None,
            "p": _f(r["p"], 4) if r is not None else None,
            "w_raw": _f(r["w_raw"]) if r is not None else None,
            "w_ewma": _f(r["w_ewma"]) if r is not None else None,
            "b_ewma": _f(r["b_ewma"]) if r is not None else None,
            "freq": _f(freq.get(f, 0), 3),
        })

    t_cfg = config.TARGETS[target]
    out = {
        "target": target, "target_name": t_cfg["name"],
        "updated_at": run["updated_at"], "source": run["source"],
        "settings": run["settings"], "last_date": dates[-1],
        "kpi": {
            "r2": _f(last["r2"]), "adj_r2": _f(last["adj_r2"]), "f_stat": _f(last["f_stat"], 2),
            "f_p": _f(last["f_p"], 6), "k": int(last["k"]), "valid": bool(last["valid"]),
            "valid_ratio": _f(st["valid"].tail(HEAT_DAYS).mean(), 3),
            **_oos(st),
        },
        "rows": rows,
        "series": {
            "dates": dates, "r2": _list(st["r2"]), "adj_r2": _list(st["adj_r2"]),
            "k": [int(v) for v in st["k"]],
            "actual": _list(st["actual"]), "pred": _list(st["pred"]),
        },
        "heat": {
            "dates": heat_dates,
            "t": {f: _list(piv["t"][f].tail(HEAT_DAYS), 2) for f in order},
            "s": {f: "".join((v if isinstance(v, str) else "N") for v in status[f].tail(HEAT_DAYS))
                  for f in order},
        },
        "tline": {f: _list(piv["t"][f], 2) for f in order},
        "weights": {f: _list(piv["w_ewma"][f], 4) for f in order},
        "names": {f: config.FACTORS.get(f, {}).get("name", f) for f in order},
        "order": order,
    }
    out["forecast"] = store.load_forecast(target)
    out["nextday"] = store.load_nextday(target)
    try:
        out["insight"] = insight.build(out, st, fd)
    except Exception:  # 해석 실패가 대시보드 전체를 막지 않도록
        logging.getLogger(__name__).exception("해석 생성 실패")
        out["insight"] = None
    return out


def today_csv_rows(target: str) -> list[list]:
    d = dashboard(target)
    if not d:
        return []
    out = [["기준일", "팩터", "그룹", "상태", "표준화베타", "t값", "p값", "당일가중치", "EWMA가중치",
            "평활베타", "최근60일채택률"]]
    label = {"S": "채택", "I": "비유의", "P": "상관제거", "N": "분산없음"}
    for r in d["rows"]:
        out.append([d["last_date"], r["name"], r["group"], label.get(r["status"], ""),
                    r["beta"], r["t"], r["p"], r["w_raw"], r["w_ewma"], r["b_ewma"], r["freq"]])
    return out


def _clean(o):
    """JSON 저장용: NaN/inf → None, numpy 스칼라 → float."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)):
        x = float(o)
        return round(x, 5) if math.isfinite(x) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def forecast_payload(target: str, fc: dict, s, last_date) -> dict:
    from core import forecast as fcm  # 순환 import 방지
    df, today = fc["records"], fc["today"]
    h = s.horizon
    last = df.iloc[-1]
    sc = fcm.scorecard(df, h)
    tname = config.TARGETS[target]["name"]
    prob, pred, sd = float(last["prob"]), float(last["pred"]), today["resid_sd"]
    period = pd.bdate_range(pd.Timestamp(last_date) + pd.Timedelta(days=1), periods=h)
    tail = df.tail(CHART_DAYS)
    return _clean({
        "target": target, "target_name": tname, "horizon": h, "asof": str(last["date"]),
        "period": [period[0].strftime("%m/%d"), period[-1].strftime("%m/%d")],
        "prob": prob, "pred": pred, "lo": pred - sd, "hi": pred + sd,
        "base": today["base"], "drivers": today["drivers"][:6], "valid": today["valid"],
        "adj_r2": today["adj_r2"], "window": today["window"], "tested": today["tested"],
        "score": sc, "verdict": fcm.verdict(sc, today, prob, tname, h),
        "series": {"dates": tail["date"].tolist(), "pred": tail["pred"].tolist(),
                   "real": tail["real"].tolist(), "prob": tail["prob"].tolist()},
    })


_WEEK = "월화수목금토일"


def _move_text(key: str, x: float) -> str:
    """팩터 원값(변화)을 사람이 읽는 단위로."""
    f = config.FACTORS.get(key, {})
    if key == "VIX":
        return f"{x:+.2f}pt"
    if f.get("transform") == "diff":           # 금리·스프레드(%) → bp
        return f"{x * 100:+.0f}bp"
    return f"{(math.exp(x) - 1) * 100:+.2f}%"   # 로그수익률 → %


def nextday_payload(target: str, s, nxt: dict | None, next_date, stats_df) -> dict:
    t = config.TARGETS[target]
    nd = pd.Timestamp(next_date)
    label = f"{nd.month}/{nd.day}({_WEEK[nd.weekday()]})"
    base = {"target": target, "mode": s.mode, "date": nd.strftime("%Y-%m-%d"), "label": label,
            "available": nxt is not None}
    if s.mode == "explain":
        base["reason"] = "설명 모드에서는 다음 세션 예상을 계산하지 않습니다."
        return _clean(base)
    if nxt is None:
        base["reason"] = ("아시아장 마감(15:30 KST) 후 갱신 때 계산됩니다."
                          if t["region"] == "US" and s.mode == "preopen"
                          else "다음 세션에 필요한 해외시장 데이터가 아직 마감되지 않았습니다.")
        return _clean(base)
    drivers = []
    for d in nxt["drivers"][:6]:
        meta = config.FACTORS.get(d["key"], {})
        drivers.append({"name": meta.get("name", d["key"]), "move": _move_text(d["key"], d["x"]),
                        "contrib": d["contrib"]})
    # 이 모드의 과거 표본외 성적(최근 60일): 방향 적중률
    st = stats_df.dropna(subset=["pred", "actual"]).tail(60)
    nz = (st["actual"] != 0) & (st["pred"] != 0)
    hit = float((np.sign(st["actual"][nz]) == np.sign(st["pred"][nz])).mean()) if nz.any() else None
    base.update({"pred": nxt["pred"], "prob": nxt["prob"], "lo": nxt["pred"] - nxt["sigma"],
                 "hi": nxt["pred"] + nxt["sigma"], "base": nxt["base"], "drivers": drivers,
                 "hit60": hit, "n60": int(nz.sum())})
    return _clean(base)
