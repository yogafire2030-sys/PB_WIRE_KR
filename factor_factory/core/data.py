"""데이터 수집 계층.

- Yahoo Finance(yfinance) 일별 종가, FRED 공개 CSV(키 불필요) 수집
- 수집 성공분은 로컬 CSV 캐시에 병합 저장 → 네트워크 장애 시 캐시로 동작
- demo=True 이면 동일한 구조의 합성 데이터를 생성 (오프라인 테스트용)
"""
from __future__ import annotations

import io
import logging
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

import config

log = logging.getLogger(__name__)

# yfinance 내부의 pandas 사용 방식(inplace=)에서 나오는 경고 — 동작과 무관하므로 숨김
warnings.filterwarnings("ignore", message=r".*inplace keyword.*")
warnings.filterwarnings("ignore", category=FutureWarning, module=r"yfinance(\..*)?")

# 일부 망에서 비브라우저 UA 요청이 지연·차단되는 경우가 있어 일반 브라우저 UA 사용
_FRED_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/csv,text/plain,*/*",
}


class DataError(RuntimeError):
    """사용자에게 그대로 보여줘도 되는 데이터 관련 오류."""


# ---------------------------------------------------------------------------
# 캐시
# ---------------------------------------------------------------------------
def _read_cache(path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        return df.sort_index() if not df.empty else None
    except (OSError, ValueError, pd.errors.ParserError):
        log.warning("캐시 파일을 읽을 수 없습니다: %s", path.name)
        return None


def _write_cache(path, df: pd.DataFrame) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    df.to_csv(tmp, float_format="%.6f")
    tmp.replace(path)  # 원자적 교체 → 쓰다 끊겨도 기존 캐시 보존


def _merge(new: pd.DataFrame | None, old: pd.DataFrame | None) -> pd.DataFrame | None:
    if new is None or new.empty:
        return old
    if old is None or old.empty:
        return new
    return new.combine_first(old).sort_index()


def _naive_dates(idx) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(pd.to_datetime(idx))
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    return idx.normalize()


# ---------------------------------------------------------------------------
# 원천 수집
# ---------------------------------------------------------------------------
def _yahoo_close(yf, tickers: list[str], start: str) -> pd.DataFrame:
    df = yf.download(tickers, start=start, auto_adjust=True, progress=False,
                     threads=True, group_by="column", timeout=15)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        close = df["Close"].copy()
    else:  # 단일 티커
        close = df[["Close"]].rename(columns={"Close": tickers[0]})
    close.index = _naive_dates(close.index)
    close = close[~close.index.duplicated(keep="last")]
    return close.astype("float64")


def fetch_yahoo(tickers: list[str], start: str) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover
        raise DataError("yfinance 패키지가 설치되어 있지 않습니다. pip install -r requirements.txt") from exc

    close = _yahoo_close(yf, tickers, start)
    # 일시적 DNS·연결 오류로 빠진 티커만 한 번 더 시도
    missing = [t for t in tickers if t not in close.columns or close[t].notna().sum() == 0]
    if missing and len(missing) < len(tickers):
        log.info("Yahoo 재시도: %s", ", ".join(missing))
        retry = _yahoo_close(yf, missing, start)
        if not retry.empty:
            close = retry.combine_first(close) if not close.empty else retry
    if close.empty:
        raise DataError("Yahoo Finance에서 데이터를 받지 못했습니다.")
    still = [t for t in tickers if t not in close.columns or close[t].notna().sum() == 0]
    if still:
        log.warning("Yahoo 수집 실패 티커(캐시로 대체): %s", ", ".join(still))
    return close.dropna(axis=1, how="all").sort_index()


def _fred_one(sid: str, start: str) -> tuple[str, pd.Series | None]:
    for attempt in range(2):
        try:
            r = requests.get(config.FRED_URL, params={"id": sid, "cosd": start},
                             timeout=config.FRED_TIMEOUT, headers=_FRED_HEADERS)
            r.raise_for_status()
            raw = pd.read_csv(io.StringIO(r.text))
            if raw.shape[1] < 2:
                return sid, None
            vals = pd.to_numeric(raw.iloc[:, 1], errors="coerce").to_numpy()
            return sid, pd.Series(vals, index=_naive_dates(raw.iloc[:, 0]))
        except requests.ConnectionError as exc:  # 접속 자체가 안 되면 재시도 무의미
            log.warning("FRED %s 접속 실패: %s", sid, type(exc).__name__)
            return sid, None
        except (requests.RequestException, ValueError, pd.errors.ParserError) as exc:
            log.warning("FRED %s 수집 실패(%d/2): %s", sid, attempt + 1, type(exc).__name__)
    return sid, None


def fetch_fred(series_ids: list[str], start: str) -> pd.DataFrame:
    """시리즈별 병렬 수집 → 전부 실패해도 최대 약 (connect+read)×2 초 안에 끝난다."""
    if not series_ids:
        return pd.DataFrame()
    with ThreadPoolExecutor(max_workers=len(series_ids)) as ex:
        results = list(ex.map(lambda sid: _fred_one(sid, start), series_ids))
    frames = {sid: ser for sid, ser in results if ser is not None}
    if not frames:
        raise DataError("FRED 데이터를 받지 못했습니다.")
    return pd.DataFrame(frames).sort_index()


def load_raw(demo: bool = False, refresh: bool = True) -> dict:
    """{'yahoo': DataFrame, 'fred': DataFrame, 'source': str} 반환."""
    if demo:
        return _demo_raw()

    y_old, f_old = _read_cache(config.YAHOO_CACHE), _read_cache(config.FRED_CACHE)
    y_new = f_new = None
    errors = []
    if refresh:
        try:
            y_new = fetch_yahoo(config.yahoo_tickers(), config.HISTORY_START)
        except Exception as exc:  # 네트워크/라이브러리 오류 모두 캐시로 대체
            log.warning("Yahoo 수집 실패: %s", exc)
            errors.append("Yahoo")
        try:
            f_new = fetch_fred(config.fred_series(), config.HISTORY_START)
        except Exception as exc:
            log.warning("FRED 수집 실패: %s", exc)
            errors.append("FRED")

    yahoo, fred = _merge(y_new, y_old), _merge(f_new, f_old)
    if yahoo is None:
        raise DataError("시세 데이터를 받지 못했고 캐시도 없습니다. 인터넷 연결(사내망 차단 여부)을 "
                        "확인하거나 '데모 데이터'로 먼저 실행해 보세요.")
    if y_new is not None:
        _write_cache(config.YAHOO_CACHE, yahoo)
    if f_new is not None:
        _write_cache(config.FRED_CACHE, fred)
    source = "live" if not errors else f"cache({','.join(errors)} 실패)"
    return {"yahoo": yahoo, "fred": fred if fred is not None else pd.DataFrame(), "source": source}


# ---------------------------------------------------------------------------
# 데모 데이터: 실제와 같은 구조 + 국면별로 유의 팩터가 바뀌도록 설계
# ---------------------------------------------------------------------------
_DEMO_REGIMES = {
    "ASIA": [
        {"SOX": 0.35, "USDKRW": -0.30, "VIX": -0.10},
        {"SPX": 0.30, "HYSPRD": -0.25, "NIKKEI": 0.20},
        {"USDKRW": -0.40, "SHCOMP": 0.25, "COPPER": 0.15},
        {"SOX": 0.25, "UST10Y": -0.20, "HSI": 0.20, "WTI": 0.10},
    ],
    "US": [
        {"VIX": -0.45, "UST10Y": -0.15, "HYSPRD": -0.20},
        {"DXY": -0.25, "COPPER": 0.20, "VIX": -0.30},
        {"HYSPRD": -0.35, "BEI10Y": 0.15, "WTI": 0.15},
        {"VIX": -0.35, "GOLD": -0.10, "T10Y2Y": 0.15},
    ],
}


def _demo_raw(seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(config.HISTORY_START, pd.Timestamp.today().normalize())
    n = len(dates)
    eq = rng.standard_normal(n)

    # 표준화된 팩터 충격
    shocks: dict[str, np.ndarray] = {}
    for key, f in config.FACTORS.items():
        if f["source"] == "tech":
            continue
        e = rng.standard_normal(n)
        if f["group"] == "주식":
            e = 0.8 * eq + 0.6 * e
        elif key == "VIX":
            e = -0.7 * eq + 0.7 * e
        shocks[key] = e

    def changes(key):
        f = config.FACTORS[key]
        if key == "VIX":
            return shocks[key] * 1.2
        return shocks[key] * (0.01 if f["transform"] == "logret" else 0.05)

    yahoo, fred = {}, {}
    regime_len = 160

    def make_target(t):
        regimes = _DEMO_REGIMES["ASIA" if t["region"] == "ASIA" else "US"]
        t_order = config.CLOSE_ORDER[t["region"]]
        y = rng.standard_normal(n) * 0.006
        for i in range(1, n):
            betas = regimes[(i // regime_len) % len(regimes)]
            for fk, b in betas.items():
                if fk in t["exclude"]:
                    continue
                lag = 1 if config.CLOSE_ORDER[config.FACTORS[fk]["region"]] > t_order else 0
                y[i] += b * 0.01 * shocks[fk][i - lag]
        return y

    # 1) 미국 지수 먼저 생성 → 같은 티커를 쓰는 팩터(S&P500)는 그 경로를 공유
    target_ids = set()
    for t in config.TARGETS.values():
        if t["region"] != "US":
            continue
        y = make_target(t)
        yahoo[t["ticker"]] = 2500 * np.exp(np.cumsum(y))
        target_ids.add(t["ticker"])
        for fk, f in config.FACTORS.items():
            if f["id"] == t["ticker"]:
                shocks[fk] = (y - y.mean()) / y.std()

    # 2) 팩터 레벨
    for key, f in config.FACTORS.items():
        if f["source"] in ("tech", "derived") or f["id"] in target_ids:
            continue
        c = changes(key)
        if f["transform"] == "logret":
            lvl = 100 * np.exp(np.cumsum(c))
        elif key == "VIX":
            lvl = np.empty(n)
            v = 18.0
            for i in range(n):  # 평균회귀
                v = max(9.0, v + 0.05 * (18.0 - v) + c[i])
                lvl[i] = v
        else:
            lvl = 3.0 + np.cumsum(c)
        (yahoo if f["source"] == "yahoo" else fred)[f["id"]] = lvl

    # 2-b) 파생 팩터: 원하는 파생 경로 D를 먼저 만들고 구성 티커를 역산
    for key, f in config.FACTORS.items():
        if f["source"] != "derived":
            continue
        c = changes(key)
        if f["op"] == "spread":
            d = np.empty(n)
            v = -1.5
            for i in range(n):
                v = v + 0.05 * (-1.5 - v) + c[i] * 0.3
                d[i] = v
            if f["a"] in yahoo:
                yahoo[f["b"]] = np.maximum(yahoo[f["a"]] - d, 1.0)
            else:
                base = yahoo.setdefault(f["b"], 20 + np.zeros(n))
                yahoo[f["a"]] = base + d
        else:
            ratio = np.exp(np.cumsum(c))
            if f["a"] in yahoo:
                yahoo[f["b"]] = yahoo[f["a"]] / ratio
            else:
                base = yahoo.setdefault(f["b"], 100 * np.exp(np.cumsum(rng.standard_normal(n) * 0.005)))
                yahoo[f["a"]] = base * ratio

    # 3) 아시아 지수: 국면별 베타 × 시차 정렬된 팩터 + 잡음
    for t in config.TARGETS.values():
        if t["region"] == "US":
            continue
        yahoo[t["ticker"]] = 2500 * np.exp(np.cumsum(make_target(t)))

    ydf = pd.DataFrame(yahoo, index=dates)
    fdf = pd.DataFrame(fred, index=dates)
    return {"yahoo": ydf, "fred": fdf, "source": "demo"}
