"""결과 자동 해석 (규칙 기반 한국어 코멘트).

출력은 순수 텍스트 리스트 → 템플릿에서 자동 이스케이프되어 렌더링된다.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats

import config

LOOKBACK = 20          # 변화 감지 구간(거래일)
SHIFT_PP = 0.05        # 가중치 변화 5%p 이상이면 '부각/약화'로 표시

# 같은 시간대에 거래돼 지수와 '동시에' 움직이는 팩터(설명 모드에서 원인이 아닌 동행 관계)
_SAME_SESSION = {
    "US": {"VIX", "HYG", "SOX", "NASDAQ", "SPX", "DXY", "UST10Y", "UST2YF", "WTI", "GOLD", "COPPER"},
    "ASIA": {"NIKKEI", "HSI", "SHCOMP"},
}


def _sign_word(b: float) -> str:
    return "같은 방향" if b > 0 else "반대 방향"


def _factor_sentence(key: str, beta: float, target_name: str) -> str:
    f = config.FACTORS.get(key, {})
    name = f.get("name", key)
    up = "상승" if beta > 0 else "하락"
    mag = f"표준화 β {beta:+.2f}"
    if key == "REV1":
        tail = ("전일 움직임이 다음 날에도 이어지는 경향(단기 추세 지속)" if beta > 0
                else "전일 움직임이 다음 날 되돌려지는 경향(단기 반전)")
        return f"「{name}」 — {tail}. 다른 팩터를 고정한 상태에서의 부분 효과입니다. ({mag})"
    if key in ("MOM5", "MOM20"):
        n = key[3:]
        tail = (f"최근 {n}일 흐름이 이어지는 경향(모멘텀)" if beta > 0
                else f"최근 {n}일 흐름이 되돌려지는 경향(평균회귀)")
        return f"「{name}」 — {tail}. ({mag})"
    if key == "RVOL":
        return f"「{name}」 — 변동성이 커질 때 {target_name} {up} 경향. ({mag})"
    if f.get("transform") == "diff":
        what = "확대" if f.get("group") in ("크레딧",) or key == "T10Y2Y" else "상승"
        return f"「{name}」 {what} 시 {target_name} {up} 경향. ({mag})"
    note = ""
    if key == "HYG":
        note = " HYG는 가격이므로 하이일드 스프레드와 부호가 반대입니다(스프레드 축소 = HYG 상승)."
    return f"「{name}」 상승 시 {target_name} {up} 경향, 즉 {_sign_word(beta)}으로 움직입니다. ({mag}){note}"


def build(d: dict, st: pd.DataFrame, fd: pd.DataFrame) -> dict:
    tkey, tname = d["target"], d["target_name"]
    region = config.TARGETS[tkey]["region"]
    s = d["settings"]
    mode = s.get("mode", "explain")
    kpi = d["kpi"]
    names = d["names"]
    rows = {r["key"]: r for r in d["rows"]}
    out: dict = {"headline": "", "drivers": [], "shifts": [], "reliability": [], "cautions": [],
                 "decomp": None}

    # ---------------- 1. 한 줄 요약 ----------------
    selected = [r for r in d["rows"] if r["status"] == "S"]
    selected.sort(key=lambda r: -abs(r["t"] or 0))
    if kpi["valid"] and selected:
        top = "·".join(names[r["key"]] for r in selected[:3])
        share = (kpi["adj_r2"] or 0) * 100
        if mode == "explain":
            out["headline"] = (f"{tname}의 최근 {s['window']}거래일 일간 등락은 {len(selected)}개 팩터로 "
                               f"약 {share:.0f}%가 설명됩니다. 핵심 동인은 {top}입니다.")
        elif mode == "preopen":
            src = "간밤 미국장·환율 등" if region == "ASIA" else "당일 아시아장·전일 미국장 등"
            out["headline"] = (f"장 시작 전에 이미 아는 정보({src})로 {tname} 당일 등락을 볼 때 "
                               f"{len(selected)}개 팩터가 유의합니다. 설명력은 약 {share:.0f}%이고, "
                               f"핵심 팩터는 {top}입니다.")
        else:
            out["headline"] = (f"전일 팩터 값으로 {tname}의 다음 날 등락을 볼 때 {len(selected)}개 팩터가 "
                               f"유의합니다. 설명력은 약 {share:.1f}%이고, 핵심 팩터는 {top}입니다.")
    else:
        if mode in ("predict", "preopen"):
            out["headline"] = (f"오늘은 다음 날 {tname} 등락을 미리 알려주는 유의한 선행 팩터가 없습니다. "
                               "일간 수익률은 대부분의 날 이렇게 나오는 것이 정상이며, 방향 판단은 상단 1주 전망을 참고하세요.")
        else:
            out["headline"] = (f"오늘은 t·F 검정을 모두 통과한 팩터 조합이 없습니다. "
                               f"{tname} 등락을 설명하는 뚜렷한 공통 동인이 보이지 않는 구간입니다.")

    # ---------------- 2. 오늘 움직임 분해 ----------------
    last = st.iloc[-1]
    today = fd[fd["date"] == d["last_date"]]
    if kpi["valid"] and pd.notna(last.get("fit")) and pd.notna(last.get("actual")):
        parts = (today[today["status"] == "S"][["factor", "contrib"]]
                 .dropna().sort_values("contrib", key=np.abs, ascending=False))
        actual, fit = float(last["actual"]), float(last["fit"])
        items = [{"key": r.factor, "name": names.get(r.factor, r.factor), "v": round(float(r.contrib), 3)}
                 for r in parts.itertuples()]
        resid = actual - fit
        base = fit - sum(i["v"] for i in items)  # 롤링 평균 수익률(상수항)
        out["decomp"] = {"actual": round(actual, 3), "fit": round(fit, 3), "resid": round(resid, 3),
                         "base": round(base, 3), "items": items}
        if items:
            lead = items[0]
            lead_txt = f"가장 크게 작용한 팩터는 「{lead['name']}」({lead['v']:+.2f}%p)"
            explained = (1 - abs(resid) / abs(actual)) * 100 if abs(actual) > 1e-9 else float("nan")
            if mode == "explain":
                txt = (f"{d['last_date']} {tname} {actual:+.2f}% 중 모델이 설명한 부분은 {fit:+.2f}%p이고, "
                       f"{lead_txt}입니다. 설명되지 않은 개별 요인은 {resid:+.2f}%p입니다.")
                if math.isfinite(explained) and abs(resid) > abs(actual) and abs(actual) > 0.3:
                    txt += " 오늘은 모델 밖의 재료(개별 이슈·수급)가 더 컸던 날입니다."
            else:
                txt = (f"{d['last_date']} {tname} 실제 {actual:+.2f}%, 모델 추정 {fit:+.2f}%입니다. "
                       f"{lead_txt}입니다.")
            out["decomp"]["text"] = txt

    # ---------------- 3. 팩터별 해석 ----------------
    for r in selected[:5]:
        sent = _factor_sentence(r["key"], r["beta"] or 0.0, tname)
        fq = r["freq"] or 0
        if fq >= 0.9:
            sent += " 최근 60일 내내 유의했던 안정적인 팩터입니다."
        elif fq < 0.3:
            sent += f" 최근 60일 중 {fq * 100:.0f}%만 유의했으니, 새로 부각되는 중인지 지켜볼 필요가 있습니다."
        out["drivers"].append(sent)

    # ---------------- 4. 변화 감지 ----------------
    w = fd.pivot(index="date", columns="factor", values="w_ewma").sort_index()
    stt = fd.pivot(index="date", columns="factor", values="status").sort_index()
    if len(w) > LOOKBACK:
        delta = (w.iloc[-1] - w.iloc[-1 - LOOKBACK]).dropna()
        for k_, v in delta.sort_values(ascending=False).items():
            if v >= SHIFT_PP:
                out["shifts"].append(f"▲ 「{names.get(k_, k_)}」 부각: 최근 {LOOKBACK}일간 가중치 "
                                     f"{w[k_].iloc[-1 - LOOKBACK] * 100:.0f}% → {w[k_].iloc[-1] * 100:.0f}%")
        for k_, v in delta.sort_values().items():
            if v <= -SHIFT_PP:
                out["shifts"].append(f"▼ 「{names.get(k_, k_)}」 약화: 최근 {LOOKBACK}일간 가중치 "
                                     f"{w[k_].iloc[-1 - LOOKBACK] * 100:.0f}% → {w[k_].iloc[-1] * 100:.0f}%")
    if len(stt) >= 2:
        prev, cur = stt.iloc[-2], stt.iloc[-1]
        new = [names.get(c, c) for c in cur.index if cur[c] == "S" and prev.get(c) != "S"]
        gone = [names.get(c, c) for c in cur.index if prev.get(c) == "S" and cur[c] != "S"]
        if new:
            out["shifts"].append("오늘 새로 유의: " + ", ".join(new))
        if gone:
            out["shifts"].append("오늘 유의성 탈락: " + ", ".join(gone))
    if not out["shifts"]:
        out["shifts"].append(f"최근 {LOOKBACK}일간 팩터 구성에 큰 변화가 없습니다(가중치 변화 5%p 미만).")

    # ---------------- 5. 모델 신뢰도 ----------------
    oos, hit, n = kpi.get("oos_r2"), kpi.get("hit"), kpi.get("n") or 0
    if mode == "explain":
        if oos is not None:
            out["reliability"].append(
                f"표본외 R² {oos:.2f}는 어제까지 추정한 모델에 오늘 팩터 값을 넣었을 때의 설명력입니다. "
                "팩터 관계가 안정적이라는 뜻이지, 내일 수익률을 맞힌다는 뜻은 아닙니다. "
                "예측력을 보려면 설정에서 ‘예측 모드’로 바꿔 보세요.")
    else:
        if hit is not None and n >= 20:
            hits = int(round(hit * n))
            p = stats.binomtest(hits, n, 0.5, alternative="greater").pvalue
            verdict = ("동전 던지기(50%)보다 통계적으로 낫습니다" if p < 0.05
                       else "아직 동전 던지기(50%)와 통계적으로 구분되지 않습니다")
            out["reliability"].append(f"최근 {n}일 방향 적중률은 {hit * 100:.0f}%(이항검정 p={p:.2f})로, {verdict}.")
        if oos is not None:
            out["reliability"].append(
                f"표본외 R² {oos:.3f}: " + ("0보다 커서 단순 평균보다 나은 예측력이 있습니다." if oos > 0
                                           else "0 이하이므로 이 기간엔 평균 수익률을 쓰는 것보다 나을 게 없습니다."))
    vr = kpi.get("valid_ratio") or 0
    k_mean = float(st["k"].tail(60).mean())
    r2_now = float(st["adj_r2"].tail(LOOKBACK).mean())
    r2_before = float(st["adj_r2"].iloc[-60 - LOOKBACK:-LOOKBACK].mean()) if len(st) > 80 else float("nan")
    msg = f"최근 60일 중 {vr * 100:.0f}% 날짜에서 모델이 F-검정을 통과했고, 평균 유의 팩터 수는 {k_mean:.1f}개입니다."
    if math.isfinite(r2_before) and math.isfinite(r2_now):
        if r2_now - r2_before > 0.05:
            msg += f" 최근 설명력이 높아지고 있습니다(조정 R² {r2_before:.2f} → {r2_now:.2f}). 매크로 팩터 장세입니다."
        elif r2_before - r2_now > 0.05:
            msg += f" 최근 설명력이 낮아지고 있습니다(조정 R² {r2_before:.2f} → {r2_now:.2f}). 개별 재료 장세에 가까워지는 중입니다."
    out["reliability"].append(msg)

    # ---------------- 6. 주의 ----------------
    if mode == "explain":
        same = [names.get(r["key"], r["key"]) for r in selected if r["key"] in _SAME_SESSION.get(region, set())]
        if same:
            out["cautions"].append(
                f"{', '.join(same)}: 대상 지수와 같은 시간에 거래되는 자산이라 동시에 움직이는 것이 자연스럽습니다. "
                "‘원인’이라기보다 같은 날 위험선호를 함께 반영한 ‘동행 지표’로 읽는 것이 안전합니다.")
    used_fallback = [names.get(k_, k_) for k_ in rows if config.FACTORS.get(k_, {}).get("fallback_for")]
    if used_fallback:
        out["cautions"].append(f"FRED 접속 실패로 대체 팩터를 사용했습니다: {', '.join(used_fallback)}.")
    if d["source"] not in ("live", "demo"):
        out["cautions"].append(f"데이터 상태: {d['source']}. 일부 시리즈는 이전에 받아둔 값입니다.")
    if d["source"] == "demo":
        out["cautions"].append("합성 데모 데이터 결과입니다. 실제 시장 해석에 쓰지 마세요.")
    return out
