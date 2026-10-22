import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.market import MarketService, fmt_time, parse_naver, parse_yahoo, INDICES


def test_fmt_time_tz_abbreviation():
    # 2026-10-22 05:12:10 UTC = 14:12:10 JST
    assert fmt_time(1792645930, "Asia/Tokyo") == "2:12:10 PM JST"
    assert fmt_time(1792645930, "Asia/Seoul").endswith("KST")


def test_parse_yahoo_down():
    now = 1792645930
    payload = {"chart": {"result": [{"meta": {
        "regularMarketPrice": 63262.75, "chartPreviousClose": 64621.24,
        "regularMarketTime": now,
        "currentTradingPeriod": {"regular": {"start": now - 3600, "end": now + 3600}},
    }}], "error": None}}
    q = parse_yahoo(payload, "N225", "NIKKEI 225", "Asia/Tokyo", now)
    assert q["change"] == -1358.49
    assert q["pct"] == -2.1
    assert q["state"] == "LAST"
    assert q["time"] == "2:12:10 PM JST"


def test_parse_naver_unsigned_fall():
    payload = {"closePrice": "2,600.50", "compareToPreviousClosePrice": "13.20",
               "compareToPreviousPrice": {"code": "5", "name": "FALLING"},
               "fluctuationsRatio": "-0.51", "localTradedAt": "2026-10-22T15:30:00+09:00",
               "marketStatus": "CLOSE"}
    q = parse_naver(payload, "KOSPI", "KOSPI", "Asia/Seoul", 0)
    assert q["change"] == -13.2
    assert q["state"] == "CLOSE"


def test_refresh_keeps_last_value_when_source_fails():
    svc = MarketService({"MARKET_POLL_INTERVAL": 15, "HTTP_TIMEOUT": 5})
    try:
        ok = {"key": "SPX", "name": "S&P 500", "price": 1.0, "change": 0.0, "pct": 0.0,
              "ts": 0, "time": "", "state": "LAST", "source": "yahoo", "stale": False}
        svc._fetch_one = lambda spec: dict(ok, key=spec[0]) if spec[0] == "SPX" else None
        svc.refresh()
        assert [q["key"] for q in svc._snapshot] == ["SPX"]
        svc._fetch_one = lambda spec: None
        svc.refresh()
        assert svc._snapshot[0]["stale"] is True
        assert len(INDICES) == 6
    finally:
        svc.stop()
