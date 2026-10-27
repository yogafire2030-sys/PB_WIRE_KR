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

# 숫자가 작을수록 먼저 마감 (한국시간 기준 하루 흐름)
CLOSE_ORDER = {"ASIA": 1, "US": 2, "FX": 2, "CRYPTO": 3}

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
# 팩터 팩토리 후보군
# source   : yahoo | fred | tech(대상 지수 자체에서 생성)
# transform: logret(로그수익률) | diff(1차 차분: 금리·스프레드·VIX 등 레벨 변수)
# ---------------------------------------------------------------------------
FACTORS: dict[str, dict] = {
    # 글로벌 주식
    "SPX":     {"source": "yahoo", "id": "^GSPC",     "region": "US",   "transform": "logret", "name": "S&P 500",          "group": "주식"},
    "NASDAQ":  {"source": "yahoo", "id": "^IXIC",     "region": "US",   "transform": "logret", "name": "나스닥 종합",        "group": "주식"},
    "SOX":     {"source": "yahoo", "id": "^SOX",      "region": "US",   "transform": "logret", "name": "필라델피아 반도체",   "group": "주식"},
    "NIKKEI":  {"source": "yahoo", "id": "^N225",     "region": "ASIA", "transform": "logret", "name": "닛케이225",          "group": "주식"},
    "HSI":     {"source": "yahoo", "id": "^HSI",      "region": "ASIA", "transform": "logret", "name": "항셍",              "group": "주식"},
    "SHCOMP":  {"source": "yahoo", "id": "000001.SS", "region": "ASIA", "transform": "logret", "name": "상해종합",           "group": "주식"},
    # 변동성
    "VIX":     {"source": "yahoo", "id": "^VIX",      "region": "US",   "transform": "diff",   "name": "VIX 변화",           "group": "변동성"},
    # 환율
    "DXY":     {"source": "yahoo", "id": "DX-Y.NYB",  "region": "US",   "transform": "logret", "name": "달러인덱스",          "group": "환율"},
    "USDKRW":  {"source": "yahoo", "id": "KRW=X",     "region": "FX",   "transform": "logret", "name": "원/달러",            "group": "환율"},
    "USDJPY":  {"source": "yahoo", "id": "JPY=X",     "region": "FX",   "transform": "logret", "name": "엔/달러",            "group": "환율"},
    # 원자재·대체
    "WTI":     {"source": "yahoo", "id": "CL=F",      "region": "US",   "transform": "logret", "name": "WTI 유가",           "group": "원자재"},
    "GOLD":    {"source": "yahoo", "id": "GC=F",      "region": "US",   "transform": "logret", "name": "금",                "group": "원자재"},
    "COPPER":  {"source": "yahoo", "id": "HG=F",      "region": "US",   "transform": "logret", "name": "구리",               "group": "원자재"},
    "BTC":     {"source": "yahoo", "id": "BTC-USD",   "region": "CRYPTO", "transform": "logret", "name": "비트코인",          "group": "원자재"},
    # 금리·크레딧 (yahoo/FRED)
    "UST10Y":  {"source": "yahoo", "id": "^TNX",      "region": "US",   "transform": "diff",   "name": "미 10년물 금리",      "group": "금리"},
    "UST2Y":   {"source": "fred",  "id": "DGS2",      "region": "US",   "transform": "diff",   "name": "미 2년물 금리",       "group": "금리"},
    "T10Y2Y":  {"source": "fred",  "id": "T10Y2Y",    "region": "US",   "transform": "diff",   "name": "장단기 금리차",       "group": "금리"},
    "BEI10Y":  {"source": "fred",  "id": "T10YIE",    "region": "US",   "transform": "diff",   "name": "10년 기대인플레",     "group": "금리"},
    "HYSPRD":  {"source": "fred",  "id": "BAMLH0A0HYM2", "region": "US", "transform": "diff",  "name": "하이일드 스프레드",   "group": "크레딧"},
    # FRED 접속이 안 될 때만 쓰는 Yahoo 대체 팩터 (fallback_for 가 가리키는 팩터가 없을 때만 포함)
    "UST2YF":  {"source": "yahoo", "id": "2YY=F",     "region": "US",   "transform": "diff",   "name": "미 2년물 금리(선물)", "group": "금리",   "fallback_for": "UST2Y"},
    "HYG":     {"source": "yahoo", "id": "HYG",       "region": "US",   "transform": "logret", "name": "하이일드 ETF(HYG)",  "group": "크레딧", "fallback_for": "HYSPRD"},
    # 기술적 팩터 (대상 지수 자체, 항상 전일까지 정보만 사용)
    "MOM5":    {"source": "tech",  "id": "mom5",      "region": "ASIA", "transform": "none",   "name": "모멘텀 5일",          "group": "기술적"},
    "MOM20":   {"source": "tech",  "id": "mom20",     "region": "ASIA", "transform": "none",   "name": "모멘텀 20일",         "group": "기술적"},
    "REV1":    {"source": "tech",  "id": "rev1",      "region": "ASIA", "transform": "none",   "name": "단기반전(전일)",      "group": "기술적"},
    "RVOL":    {"source": "tech",  "id": "rvol",      "region": "ASIA", "transform": "none",   "name": "실현변동성 변화",     "group": "기술적"},
}


def yahoo_tickers() -> list[str]:
    ids = {t["ticker"] for t in TARGETS.values()}
    ids |= {f["id"] for f in FACTORS.values() if f["source"] == "yahoo"}
    return sorted(ids)


def fred_series() -> list[str]:
    return sorted({f["id"] for f in FACTORS.values() if f["source"] == "fred"})
