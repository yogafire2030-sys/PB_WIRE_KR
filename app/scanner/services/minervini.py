"""마크 미너비니 Trend Template 판정 엔진.

Finviz 컬럼은 이동평균/52주 고저가를 '현재가 대비 %'로 제공합니다.
  SMA50 = Price / (1 + d50/100)  →  SMA50 > SMA200  ⇔  d50 < d200
이 관계를 이용해 원시 가격 데이터 없이도 조건을 정확히 판정합니다.
"""
from typing import Optional

CHECK_DEFS = [
    ("above_sma50", "가격 > 50일선"),
    ("above_sma200", "가격 > 200일선 (150일선 대용)"),
    ("sma_stack", "50일선 > 200일선"),
    ("above_low", "52주 저가 대비 충분한 상승"),
    ("near_high", "52주 고가 근접"),
    ("rs_proxy", "1년 수익률 기준 충족 (RS 대용)"),
    ("sma200_rising", "6개월 수익률 > 0 (200일선 상승 추정)"),
]


def _gt(a: Optional[float], b: float) -> Optional[bool]:
    return None if a is None else a > b


def evaluate(row: dict, params: dict) -> dict:
    d50, d200 = row.get("sma50"), row.get("sma200")
    checks = {
        "above_sma50": _gt(d50, 0),
        "above_sma200": _gt(d200, 0),
        "sma_stack": None if d50 is None or d200 is None else d50 < d200,
        "above_low": None if row.get("low52") is None else row["low52"] >= params["min_above_low"],
        "near_high": None if row.get("high52") is None else row["high52"] >= -params["max_below_high"],
        "rs_proxy": None if row.get("perf_y") is None else row["perf_y"] >= params["min_perf_year"],
        "sma200_rising": _gt(row.get("perf_h"), 0),
    }
    passed = sum(1 for v in checks.values() if v is True)
    return {"checks": checks, "passed": passed, "total": len(checks)}


def rs_raw(row: dict) -> Optional[float]:
    """IBD RS와 유사한 가중 수익률 (최근 구간 가중)."""
    q, h, y = row.get("perf_q"), row.get("perf_h"), row.get("perf_y")
    if q is None and h is None and y is None:
        return None
    return 0.4 * (q or 0.0) + 0.3 * (h or 0.0) + 0.3 * (y or 0.0)


def apply(rows: list, params: dict) -> list:
    """판정/RS 백분위를 계산하고 정렬된 결과를 반환."""
    for r in rows:
        r.update(evaluate(r, params))
        r["rs_raw"] = rs_raw(r)

    # 결과 집합 내 RS 백분위 (1~99, 동점은 같은 순위) - O(n log n)
    scored = sorted((r for r in rows if r["rs_raw"] is not None), key=lambda r: r["rs_raw"])
    n = len(scored)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and scored[j + 1]["rs_raw"] == scored[i]["rs_raw"]:
            j += 1
        rank = int(round(1 + 98 * ((i + j) / 2) / (n - 1))) if n > 1 else 99
        for k in range(i, j + 1):
            scored[k]["rs_rank"] = rank
        i = j + 1
    for r in rows:
        r.setdefault("rs_rank", None)

    if params.get("strict"):
        rows = [r for r in rows if r["passed"] == r["total"]]
    rows.sort(key=lambda r: (r["passed"], r["rs_raw"] if r["rs_raw"] is not None else -1e9), reverse=True)
    return rows
