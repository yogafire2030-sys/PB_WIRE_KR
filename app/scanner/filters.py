"""스크리닝 조건 정의와 입력 검증(허용된 값·범위만 사용)."""

UNIVERSES = [("all", "S&P 500 + 나스닥 100"), ("sp500", "S&P 500"), ("ndx", "나스닥 100")]
MIN_PRICE = [(0, "제한 없음"), (5, "$5 초과"), (10, "$10 초과"), (20, "$20 초과"), (50, "$50 초과")]
MIN_VOLUME = [(0, "제한 없음"), (200_000, "20만주 초과"), (500_000, "50만주 초과"),
              (1_000_000, "100만주 초과"), (3_000_000, "300만주 초과")]

SELECTS = [
    {"key": "universe", "label": "대상 종목", "default": "all", "options": UNIVERSES,
     "hint": "구성 종목은 하루 1회 자동 갱신"},
    {"key": "min_price", "label": "주가", "default": 10, "options": MIN_PRICE, "hint": "저가주 제외"},
    {"key": "min_volume", "label": "평균 거래량 (50일)", "default": 0, "options": MIN_VOLUME, "hint": ""},
]

POST_PARAMS = [
    {"key": "min_above_low", "label": "52주 저가 대비 최소 상승률 (%)", "default": 30, "min": 0, "max": 1000,
     "hint": "미너비니 기준 30% 이상"},
    {"key": "max_below_high", "label": "52주 고가 대비 최대 하락률 (%)", "default": 25, "min": 0, "max": 100,
     "hint": "미너비니 기준 25% 이내"},
    {"key": "min_rs", "label": "RS 최소 (1~99)", "default": 70, "min": 1, "max": 99,
     "hint": "미너비니 기준 70 이상 · 대상 종목 내 백분위"},
]


def sanitize(raw: dict) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for s in SELECTS:
        allowed = {v for v, _ in s["options"]}
        v = raw.get(s["key"], s["default"])
        if isinstance(s["default"], int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = s["default"]
        out[s["key"]] = v if v in allowed else s["default"]
    for p in POST_PARAMS:
        try:
            v = float(raw.get(p["key"], p["default"]))
        except (TypeError, ValueError):
            v = float(p["default"])
        if v != v:  # NaN
            v = float(p["default"])
        out[p["key"]] = min(max(v, p["min"]), p["max"])
    out["strict"] = raw.get("strict") is True
    return out
