"""Factor Factory 전역 설정.

- TARGETS : 종속변수(지수) 정의
- FACTORS : 팩터 팩토리의 후보 독립변수 정의
- CLOSE_ORDER : 시장 마감 순서. 같은 날짜라도 대상 지수보다 늦게 마감하는
  시장의 값은 '전일' 값을 쓰도록 하여 미래정보(look-ahead) 혼입을 막는다.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FACTOR_DATA_DIR", BASE_DIR / "data")).resolve()
DB_PATH = DATA_DIR / "factor_factory.db"
YAHOO_CACHE = DATA_DIR / "cache_yahoo_close.csv"
FRED_CACHE = DATA_DIR / "cache_fred.csv"

HISTORY_START = "2016-01-01"
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_TIMEOUT = (5, 12)  # (접속, 응답) 초

# 숫자가 작을수록 먼저 마감 (한국시간 기준 하루 흐름: 아시아 → 유럽 → 미국·환율 → 코인)
CLOSE_ORDER = {"ASIA": 1, "EU": 1.5, "US": 2, "FX": 2, "CRYPTO": 3}

# ---------------------------------------------------------------------------
# 종속변수(대상 지수)
# exclude : 대상과 사실상 같은 자산이라 설명력이 무의미한 팩터
# ---------------------------------------------------------------------------
TARGETS: dict[str, dict] = {
    "KOSPI": {"ticker": "^KS11", "region": "ASIA", "name": "KOSPI", "exclude": ()},
    "KOSDAQ": {"ticker": "^KQ11", "region": "ASIA", "name": "KOSDAQ", "exclude": ()},
    "SPX": {"ticker": "^GSPC", "region": "US", "name": "S&P 500", "exclude": ("SPX", "NASDAQ")},
    "NDX": {"ticker": "^NDX", "region": "US", "name": "NASDAQ 100", "exclude": ("SPX", "NASDAQ", "SOX")},
}
DEFAULT_TARGET = "KOSPI"

# ---------------------------------------------------------------------------
# 팩터 아카이브 (레지스트리)
#
# 필수: source, id, region, transform, name, group, why(경제적 근거 한 줄)
# 선택: status   core(검증된 기본) | candidate(신규 후보, 성적 관찰 중) | retired(은퇴: 계산 제외, 기록 보존)
#       added    등록일 (아카이브 페이지에서 '관찰 기간' 계산)
#       unit     화면 표시 단위: % | bp | pt   (기본: logret→%, diff→bp)
#       fallback_for  원래 팩터를 못 받을 때만 쓰는 대체 팩터
#       op, a, b 파생 팩터(source="derived"): a/b(ratio) 또는 a−b(spread) — Yahoo 티커 2개로 계산
#
# 원칙: ① 근거(why) 없는 팩터는 등록하지 않는다
#       ② 이미 있는 팩터들의 단순 선형결합(예: 구리·금이 모두 있는데 구리/금 비율)은 넣지 않는다
#       ③ 신규는 candidate로 넣고 60거래일 뒤 아카이브 성적표로 core/retired 결정
# ---------------------------------------------------------------------------
def _f(source, id_, region, transform, name, group, why, status="core", added="2026-10-27", **kw):
    return {"source": source, "id": id_, "region": region, "transform": transform, "name": name,
            "group": group, "why": why, "status": status, "added": added, **kw}


_NEW = {"status": "candidate", "added": "2026-10-28"}

FACTORS: dict[str, dict] = {
    # ── 글로벌 주식 ──
    "SPX":     _f("yahoo", "^GSPC", "US", "logret", "S&P 500", "주식", "글로벌 위험선호의 기준점"),
    "NASDAQ":  _f("yahoo", "^IXIC", "US", "logret", "나스닥 종합", "주식", "성장·기술주 심리"),
    "SOX":     _f("yahoo", "^SOX", "US", "logret", "필라델피아 반도체", "주식", "한국 시총 상위 반도체 업황 직결"),
    "NIKKEI":  _f("yahoo", "^N225", "ASIA", "logret", "닛케이225", "주식", "동시간대 아시아 위험선호"),
    "HSI":     _f("yahoo", "^HSI", "ASIA", "logret", "항셍", "주식", "중국 관련 외국인 자금 흐름"),
    "SHCOMP":  _f("yahoo", "000001.SS", "ASIA", "logret", "상해종합", "주식", "중국 내수 경기 기대"),
    "EWY":     _f("yahoo", "EWY", "US", "logret", "한국 ETF(EWY, 미국상장)", "주식",
                  "간밤 미국 시장 참가자가 본 한국 주식 — 외국인 시각의 선행 지표", **_NEW),
    "TWII":    _f("yahoo", "^TWII", "ASIA", "logret", "대만 가권", "주식",
                  "TSMC 중심 반도체 공급망 동조, 외국인 아시아 테크 수급", **_NEW),
    "STOXX50": _f("yahoo", "^STOXX50E", "EU", "logret", "유로스톡스50", "주식",
                  "아시아 마감 후·미국 개장 전 글로벌 위험선호의 중간 다리", **_NEW),
    "RUT":     _f("yahoo", "^RUT", "US", "logret", "러셀2000(소형주)", "주식",
                  "경기민감·금리민감 소형주 — 위험선호 폭의 넓이", **_NEW),
    # ── 변동성 ──
    "VIX":     _f("yahoo", "^VIX", "US", "diff", "VIX 변화", "변동성", "주식 공포지수", unit="pt"),
    "VIXTERM": _f("derived", "VIX-VIX3M", "US", "diff", "VIX 기간구조(1M−3M)", "변동성",
                  "단기 공포가 중기보다 높아지는(역전) 순간이 패닉 국면 — 레벨보다 구조 변화가 신호",
                  op="spread", a="^VIX", b="^VIX3M", unit="pt", **_NEW),
    "MOVE":    _f("yahoo", "^MOVE", "US", "diff", "MOVE(채권 변동성)", "변동성",
                  "금리 변동성 확대는 할인율 불확실성 → 밸류에이션 압박", unit="pt", **_NEW),
    # ── 환율 ──
    "DXY":     _f("yahoo", "DX-Y.NYB", "US", "logret", "달러인덱스", "환율", "달러 강세 = 신흥국 자금 이탈"),
    "USDKRW":  _f("yahoo", "KRW=X", "FX", "logret", "원/달러", "환율", "외국인 환차손·수급"),
    "USDJPY":  _f("yahoo", "JPY=X", "FX", "logret", "엔/달러", "환율", "엔 캐리·일본 수출주 경쟁"),
    "USDCNH":  _f("yahoo", "CNH=X", "FX", "logret", "달러/위안(역외)", "환율",
                  "중국 리스크·원화와의 동조 — 위안 약세는 원화 약세로 전이", **_NEW),
    "AUDJPY":  _f("yahoo", "AUDJPY=X", "FX", "logret", "호주달러/엔", "환율",
                  "원자재통화 vs 안전통화 — 대표적 위험선호 온도계", **_NEW),
    # ── 원자재·대체 ──
    "WTI":     _f("yahoo", "CL=F", "US", "logret", "WTI 유가", "원자재", "인플레·비용 압력"),
    "GOLD":    _f("yahoo", "GC=F", "US", "logret", "금", "원자재", "안전자산 선호"),
    "COPPER":  _f("yahoo", "HG=F", "US", "logret", "구리", "원자재", "글로벌 제조업 경기"),
    "BTC":     _f("yahoo", "BTC-USD", "CRYPTO", "logret", "비트코인", "원자재", "개인 투기심리·유동성"),
    # ── 금리 ──
    "UST10Y":  _f("yahoo", "^TNX", "US", "diff", "미 10년물 금리", "금리", "할인율·밸류에이션"),
    "UST3M":   _f("yahoo", "^IRX", "US", "diff", "미 3개월물 금리", "금리",
                  "단기 정책금리 기대 — Fed 경로 재평가를 가장 빨리 반영", **_NEW),
    "UST2Y":   _f("fred", "DGS2", "US", "diff", "미 2년물 금리", "금리", "Fed 정책 기대"),
    "T10Y2Y":  _f("fred", "T10Y2Y", "US", "diff", "장단기 금리차", "금리", "경기 사이클 기대"),
    "BEI10Y":  _f("fred", "T10YIE", "US", "diff", "10년 기대인플레", "금리", "인플레 기대"),
    # ── 크레딧 ──
    "HYSPRD":  _f("fred", "BAMLH0A0HYM2", "US", "diff", "하이일드 스프레드", "크레딧", "신용위험 프리미엄"),
    "CREDIT":  _f("derived", "HYG/LQD", "US", "logret", "하이일드/투자등급 상대강도", "크레딧",
                  "같은 회사채 안에서 위험등급 선호 — 금리 효과를 걷어낸 순수 신용 위험선호",
                  op="ratio", a="HYG", b="LQD", **_NEW),
    "EMB":     _f("yahoo", "EMB", "US", "logret", "신흥국 달러채(EMB)", "크레딧",
                  "신흥국 신용 여건 — 한국 등 EM 자금흐름과 동행", **_NEW),
    # ── FRED 대체 (원래 팩터를 못 받을 때만) ──
    "UST2YF":  _f("yahoo", "2YY=F", "US", "diff", "미 2년물 금리(선물)", "금리", "DGS2 대체", fallback_for="UST2Y"),
    "HYG":     _f("yahoo", "HYG", "US", "logret", "하이일드 ETF(HYG)", "크레딧", "하이일드 스프레드 대체(부호 반대)",
                  fallback_for="HYSPRD"),
    # ── 기술적 (대상 지수 자체, 항상 전일까지 정보만) ──
    "MOM5":    _f("tech", "mom5", "ASIA", "none", "모멘텀 5일", "기술적", "단기 추세 지속/반전"),
    "MOM20":   _f("tech", "mom20", "ASIA", "none", "모멘텀 20일", "기술적", "중기 추세"),
    "REV1":    _f("tech", "rev1", "ASIA", "none", "단기반전(전일)", "기술적", "전일 과잉반응의 되돌림"),
    "RVOL":    _f("tech", "rvol", "ASIA", "none", "실현변동성 변화", "기술적", "변동성 레짐 전환"),
}


def active_factors() -> dict[str, dict]:
    """모델에 투입되는 팩터(은퇴 제외)."""
    return {k: f for k, f in FACTORS.items() if f.get("status") != "retired"}


def registry_problems() -> list[str]:
    """레지스트리 규칙 점검 (테스트·README용)."""
    probs = []
    act = active_factors()
    ids = {f["id"]: k for k, f in act.items() if f["source"] == "yahoo"}
    for k, f in FACTORS.items():
        if not f.get("why"):
            probs.append(f"{k}: 경제적 근거(why)가 없습니다")
        if f["source"] == "derived":
            if f.get("op") not in ("ratio", "spread") or not f.get("a") or not f.get("b"):
                probs.append(f"{k}: 파생 팩터 정의(op/a/b)가 잘못됐습니다")
            elif f["status"] != "retired" and f["a"] in ids and f["b"] in ids \
                    and not act[ids[f["a"]]].get("fallback_for") and not act[ids[f["b"]]].get("fallback_for"):
                probs.append(f"{k}: 구성요소 {f['a']}·{f['b']}가 모두 개별 팩터라 선형결합입니다")
    return probs


def yahoo_tickers() -> list[str]:
    ids = {t["ticker"] for t in TARGETS.values()}
    for f in active_factors().values():
        if f["source"] == "yahoo":
            ids.add(f["id"])
        elif f["source"] == "derived":
            ids |= {f["a"], f["b"]}
    return sorted(ids)


def fred_series() -> list[str]:
    return sorted({f["id"] for f in active_factors().values() if f["source"] == "fred"})
