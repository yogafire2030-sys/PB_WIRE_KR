"""Finviz 스크리너 필터 정의 (마크 미너비니 SEPA / Trend Template 기준).

서버는 여기 정의된 코드만 허용(화이트리스트)하며,
'고급 코드'는 엄격한 정규식을 통과한 값만 받습니다.
"""
import re

# 각 필터: key, label, hint, default, options[(code, label)]
FILTER_GROUPS = [
    {
        "id": "trend",
        "title": "추세 템플릿",
        "subtitle": "Stage 2 상승 추세 조건",
        "filters": [
            {
                "key": "sma50", "label": "가격 vs 50일선", "default": "ta_sma50_pa",
                "hint": "주가가 50일 이동평균 위",
                "options": [("", "제한 없음"), ("ta_sma50_pa", "가격 > 50일선")],
            },
            {
                "key": "sma200", "label": "가격 vs 200일선", "default": "ta_sma200_pa",
                "hint": "주가가 200일 이동평균 위 (150일선은 Finviz 미지원 → 200일선으로 대체)",
                "options": [("", "제한 없음"), ("ta_sma200_pa", "가격 > 200일선")],
            },
            {
                "key": "sma_stack", "label": "50일선 vs 200일선", "default": "ta_sma200_sb50",
                "hint": "이동평균 정배열",
                "options": [("", "제한 없음"), ("ta_sma200_sb50", "50일선 > 200일선")],
            },
            {
                "key": "high52", "label": "52주 고가 대비", "default": "ta_highlow52w_0to25-bhx",
                "hint": "미너비니 기준: 52주 고가의 25% 이내",
                "options": [
                    ("", "제한 없음"),
                    ("ta_highlow52w_0to25-bhx", "고가 대비 0~25% 이내"),
                    ("ta_highlow52w_b0to10h", "고가 대비 0~10% 이내"),
                    ("ta_highlow52w_b0to3h", "고가 대비 0~3% 이내"),
                ],
            },
        ],
    },
    {
        "id": "fund",
        "title": "펀더멘털",
        "subtitle": "SEPA 실적 성장 조건",
        "filters": [
            {
                "key": "epsqoq", "label": "EPS 성장 (분기 YoY)", "default": "fa_epsqoq_o25",
                "hint": "최근 분기 EPS 성장률",
                "options": [
                    ("", "제한 없음"), ("fa_epsqoq_pos", "양수 (> 0%)"),
                    ("fa_epsqoq_o15", "15% 초과"), ("fa_epsqoq_o20", "20% 초과"),
                    ("fa_epsqoq_o25", "25% 초과"), ("fa_epsqoq_o30", "30% 초과"),
                ],
            },
            {
                "key": "salesqoq", "label": "매출 성장 (분기 YoY)", "default": "fa_salesqoq_o15",
                "hint": "최근 분기 매출 성장률",
                "options": [
                    ("", "제한 없음"), ("fa_salesqoq_pos", "양수 (> 0%)"),
                    ("fa_salesqoq_o15", "15% 초과"), ("fa_salesqoq_o20", "20% 초과"),
                    ("fa_salesqoq_o25", "25% 초과"),
                ],
            },
            {
                "key": "epsyoy", "label": "EPS 성장 (올해)", "default": "",
                "hint": "당해 연도 EPS 성장률",
                "options": [
                    ("", "제한 없음"), ("fa_epsyoy_pos", "양수 (> 0%)"),
                    ("fa_epsyoy_o20", "20% 초과"), ("fa_epsyoy_o25", "25% 초과"),
                ],
            },
            {
                "key": "roe", "label": "ROE", "default": "fa_roe_o15",
                "hint": "미너비니 선호: 17% 이상",
                "options": [("", "제한 없음"), ("fa_roe_pos", "양수"), ("fa_roe_o15", "15% 초과"), ("fa_roe_o20", "20% 초과")],
            },
        ],
    },
    {
        "id": "liq",
        "title": "유동성 · 규모",
        "subtitle": "기관 매수가 가능한 종목",
        "filters": [
            {
                "key": "price", "label": "주가", "default": "sh_price_o10",
                "hint": "저가주 제외",
                "options": [("", "제한 없음"), ("sh_price_o5", "$5 초과"), ("sh_price_o10", "$10 초과"),
                            ("sh_price_o15", "$15 초과"), ("sh_price_o20", "$20 초과")],
            },
            {
                "key": "avgvol", "label": "평균 거래량", "default": "sh_avgvol_o200",
                "hint": "일평균 거래량",
                "options": [("", "제한 없음"), ("sh_avgvol_o200", "20만주 초과"), ("sh_avgvol_o300", "30만주 초과"),
                            ("sh_avgvol_o500", "50만주 초과"), ("sh_avgvol_o1000", "100만주 초과")],
            },
            {
                "key": "cap", "label": "시가총액", "default": "cap_smallover",
                "hint": "",
                "options": [("", "제한 없음"), ("cap_smallover", "소형주 이상 ($300M+)"),
                            ("cap_midover", "중형주 이상 ($2B+)"), ("cap_large", "대형주 ($10B~$200B)")],
            },
            {
                "key": "geo", "label": "상장 국가", "default": "",
                "hint": "",
                "options": [("", "전체 (ADR 포함)"), ("geo_usa", "미국 기업만")],
            },
        ],
    },
]

# 후처리(Finviz 데이터 컬럼으로 정밀 판정) 기본값
POST_PARAMS = [
    {"key": "min_above_low", "label": "52주 저가 대비 최소 상승률 (%)", "default": 30, "min": 0, "max": 1000,
     "hint": "미너비니 기준 30% 이상"},
    {"key": "max_below_high", "label": "52주 고가 대비 최대 하락률 (%)", "default": 25, "min": 0, "max": 100,
     "hint": "미너비니 기준 25% 이내"},
    {"key": "min_perf_year", "label": "1년 수익률 최소 (%) · RS 대용", "default": 25, "min": -100, "max": 10000,
     "hint": "상대강도(RS) 상위 종목 근사"},
]

ALLOWED_CODES = frozenset(
    code for g in FILTER_GROUPS for f in g["filters"] for code, _ in f["options"] if code
)
FILTER_KEYS = {f["key"]: f for g in FILTER_GROUPS for f in g["filters"]}

# 고급 사용자 직접 입력 코드: 소문자/숫자/._- 만, 영역 접두사 필수
_CUSTOM_RE = re.compile(r"^(?:ta|fa|sh|an|cap|geo|sec|ind|exch|idx|earningsdate|ipodate|theme)_[a-z0-9][a-z0-9._\-]{0,40}$")
MAX_CUSTOM_CODES = 15


def sanitize_filters(selected: dict, custom: str) -> list:
    """사용자 선택값을 검증하여 Finviz 필터 코드 리스트로 반환."""
    codes = []
    if isinstance(selected, dict):
        for key, code in selected.items():
            if key in FILTER_KEYS and isinstance(code, str) and code in ALLOWED_CODES:
                valid_for_key = {c for c, _ in FILTER_KEYS[key]["options"]}
                if code in valid_for_key:
                    codes.append(code)
    if isinstance(custom, str) and custom.strip():
        parts = [p.strip().lower() for p in re.split(r"[,\s]+", custom[:800]) if p.strip()]
        if len(parts) > MAX_CUSTOM_CODES:
            raise ValueError(f"고급 코드는 최대 {MAX_CUSTOM_CODES}개까지 입력할 수 있습니다.")
        for p in parts:
            if not _CUSTOM_RE.match(p):
                raise ValueError(f"허용되지 않는 필터 코드 형식입니다: {p[:40]}")
            codes.append(p)
    # 순서 유지 중복 제거
    return list(dict.fromkeys(codes))


def sanitize_post_params(raw: dict) -> dict:
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for p in POST_PARAMS:
        try:
            v = float(raw.get(p["key"], p["default"]))
        except (TypeError, ValueError):
            v = float(p["default"])
        if v != v:  # NaN
            v = float(p["default"])
        out[p["key"]] = min(max(v, p["min"]), p["max"])
    out["strict"] = bool(raw.get("strict", False))
    return out
