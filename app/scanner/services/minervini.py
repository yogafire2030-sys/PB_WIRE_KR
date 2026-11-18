"""마크 미너비니 Trend Template 판정 엔진 (Yahoo 일봉으로 직접 계산).

원래의 8개 조건을 실제 이동평균으로 판정합니다.
  1 주가 > 150일선, 200일선      2 150일선 > 200일선
  3 200일선 최소 1개월 상승       4 50일선 > 150일선, 200일선
  5 주가 > 50일선                 6 52주 저가 대비 30% 이상 상승
  7 52주 고가 대비 25% 이내       8 RS 70 이상(대상 종목 내 백분위)
6~8의 기준값은 화면에서 조정 가능합니다.
"""
from __future__ import annotations

from typing import Optional

CHECK_DEFS = [
    ("above_150_200", "주가 > 150일선 · 200일선"),
    ("sma150_gt_200", "150일선 > 200일선"),
    ("sma200_rising", "200일선 1개월 이상 상승"),
    ("sma50_gt_150_200", "50일선 > 150일선 · 200일선"),
    ("above_sma50", "주가 > 50일선"),
    ("above_low", "52주 저가 대비 충분한 상승"),
    ("near_high", "52주 고가 근접"),
    ("rs", "RS 기준 충족"),
]
MONTH = 22  # 1개월 ≈ 22거래일


def _sma(c: list, n: int, back: int = 0) -> Optional[float]:
    end = len(c) - back
    if end < n:
        return None
    return sum(c[end - n:end]) / n


def _ret(c: list, price: float, n: int) -> Optional[float]:
    return (price / c[-1 - n] - 1) * 100 if len(c) > n and c[-1 - n] > 0 else None


def _rsi(c: list, n: int = 14) -> Optional[float]:
    if len(c) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):  # Wilder 평활
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or not b else (a / b - 1) * 100


def compute(ticker: str, payload: dict) -> Optional[dict]:
    """Yahoo chart(1년 일봉) 응답 → 판정에 필요한 지표. 데이터 부족 시 None."""
    res = payload["chart"]["result"][0]
    meta = res.get("meta") or {}
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    rows = [(c, h, l, v) for c, h, l, v in zip(q.get("close") or [], q.get("high") or [],
                                              q.get("low") or [], q.get("volume") or [])
            if isinstance(c, (int, float)) and c > 0]
    if len(rows) < 60:
        return None
    c = [r[0] for r in rows]
    highs = [r[1] if isinstance(r[1], (int, float)) else r[0] for r in rows][-252:]
    lows = [r[2] if isinstance(r[2], (int, float)) else r[0] for r in rows][-252:]
    vols = [r[3] for r in rows if isinstance(r[3], (int, float))]
    price = meta.get("regularMarketPrice")
    price = float(price) if isinstance(price, (int, float)) and price > 0 else c[-1]

    sma50, sma150, sma200 = _sma(c, 50), _sma(c, 150), _sma(c, 200)
    high52, low52 = max(highs), min(lows)
    avg50 = sum(vols[-50:]) / len(vols[-50:]) if vols else None
    r63, r126, r189, r252 = (_ret(c, price, n) for n in (63, 126, 189, 252))
    # IBD 방식 가중 수익률(최근 분기 40%, 이전 3개 분기 각 20%) — 1년치가 없으면 계산 안 함
    rs_raw = (0.4 * r63 + 0.2 * r126 + 0.2 * r189 + 0.2 * r252) if None not in (r63, r126, r189, r252) else None
    return {
        "ticker": ticker,
        "company": str(meta.get("longName") or meta.get("shortName") or "")[:80],
        "exchange": str(meta.get("fullExchangeName") or meta.get("exchangeName") or "")[:30],
        "price": round(price, 2),
        "change": round((c[-1] / c[-2] - 1) * 100, 2) if len(c) > 1 and c[-2] else None,
        "sma50": sma50, "sma150": sma150, "sma200": sma200,
        "sma200_1m": _sma(c, 200, MONTH),
        "sma50_pct": _pct(price, sma50), "sma150_pct": _pct(price, sma150), "sma200_pct": _pct(price, sma200),
        "high52": high52, "low52": low52,
        "from_high": _pct(price, high52),   # 0 이하(고가 대비 하락률)
        "from_low": _pct(price, low52),     # 0 이상(저가 대비 상승률)
        "perf_q": r63, "perf_h": r126, "perf_y": r252,
        "rsi": _rsi(c[-120:]),
        "avg_volume": avg50,
        "rel_volume": (vols[-1] / avg50) if vols and avg50 else None,
        "rs_raw": rs_raw,
        "rs_rank": None,
    }


def rank_rs(rows: list) -> None:
    """대상 종목 전체 내 RS 백분위(1~99, 동점 같은 순위) — O(n log n)."""
    scored = sorted((r for r in rows if r["rs_raw"] is not None), key=lambda r: r["rs_raw"])
    n, i = len(scored), 0
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1]["rs_raw"] == scored[i]["rs_raw"]:
            j += 1
        rank = int(round(1 + 98 * ((i + j) / 2) / (n - 1))) if n > 1 else 99
        for k in range(i, j + 1):
            scored[k]["rs_rank"] = rank
        i = j + 1


def _and(*vals) -> Optional[bool]:
    return None if any(v is None for v in vals) else all(vals)


def evaluate(r: dict, params: dict) -> dict:
    p, s50, s150, s200 = r["price"], r["sma50"], r["sma150"], r["sma200"]
    gt = lambda a, b: None if a is None or b is None else a > b  # noqa: E731
    checks = {
        "above_150_200": _and(gt(p, s150), gt(p, s200)),
        "sma150_gt_200": gt(s150, s200),
        "sma200_rising": gt(s200, r["sma200_1m"]),
        "sma50_gt_150_200": _and(gt(s50, s150), gt(s50, s200)),
        "above_sma50": gt(p, s50),
        "above_low": None if r["from_low"] is None else r["from_low"] >= params["min_above_low"],
        "near_high": None if r["from_high"] is None else r["from_high"] >= -params["max_below_high"],
        "rs": None if r["rs_rank"] is None else r["rs_rank"] >= params["min_rs"],
    }
    return {"checks": checks, "passed": sum(1 for v in checks.values() if v is True), "total": len(checks)}


def apply(rows: list, params: dict) -> list:
    """판정 후 조건 충족 수 → RS 순으로 정렬(원본 행은 변경하지 않음)."""
    out = [{**r, **evaluate(r, params)} for r in rows]
    if params.get("strict"):
        out = [r for r in out if r["passed"] == r["total"]]
    out.sort(key=lambda r: (r["passed"], r["rs_raw"] if r["rs_raw"] is not None else -1e9), reverse=True)
    return out
