"""스크리너(/scanner) — 모의(mock) 응답으로 전체 흐름 검증."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.scanner.services import finviz, http_client, minervini, stocktwits  # noqa: E402
from config import Config  # noqa: E402

HEAD = ["No.", "Ticker", "Company", "Sector", "Industry", "Market Cap", "P/E", "EPS this Y", "EPS Q/Q",
        "Sales Q/Q", "Inst Own", "ROE", "Perf Week", "Perf Month", "Perf Quart", "Perf Half", "Perf Year",
        "SMA50", "SMA200", "52W High", "52W Low", "RSI", "Avg Volume", "Rel Volume", "Price", "Change",
        "Volume", "Earnings"]


def _row(i, tk, sma50, sma200, high, low, perf_y, perf_h="12.00%"):
    return [str(i), tk, f"{tk} Corp", "Technology", "Software", "12.34B", "35.1", "40.00%", "55.00%", "30.00%",
            "70.00%", "25.00%", "2.00%", "8.00%", "20.00%", perf_h, perf_y, sma50, sma200, high, low,
            "65.00", "1.20M", "1.30", "120.50", "1.25%", "1,500,000", "Oct 28 AMC"]


def finviz_html(rows, total):
    th = "".join(f"<th>{h}</th>" for h in HEAD)
    body = "".join("<tr>" + "".join(f"<td><a>{c}</a></td>" for c in r) + "</tr>" for r in rows)
    return (f"<html><body><div>#1 / {total} Total</div><table><tr><td><table class='screener_table'>"
            f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></td></tr></table></body></html>")


class FakeResp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data

    def close(self):
        pass


class FakeSession:
    def __init__(self):
        self.calls = []
        now = datetime.now(timezone.utc)
        self.now = now

    def close(self):
        pass

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, params))
        if "finviz.com" in url:
            if "r=21" in url:
                return FakeResp(text=finviz_html([_row(21, "ZZZ", "3.00%", "10.00%", "-5.00%", "50.00%", "40.00%")], 21))
            rows = [_row(1, "NVDA", "5.00%", "30.00%", "-3.00%", "120.00%", "150.00%")]
            rows.append(_row(2, "BAD", "-2.00%", "5.00%", "-40.00%", "10.00%", "-5.00%", "-3.00%"))
            rows += [_row(i, f"T{i:02d}"[:4], "4.00%", "15.00%", "-8.00%", "60.00%", "45.00%") for i in range(3, 21)]
            return FakeResp(text=finviz_html(rows, 21))
        if "trending" in url:
            return FakeResp(data={"symbols": [{"symbol": "NVDA"}]})
        if "streams/symbol/NVDA" in url:
            n = self.now
            msgs = [{"created_at": (n - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "entities": {"sentiment": {"basic": "Bullish" if h % 2 else "Bearish"}}} for h in
                    [1, 2, 3, 4, 5, 6, 30, 50]]
            return FakeResp(data={"symbol": {"watchlist_count": 900000, "title": "NVIDIA"},
                                  "cursor": {"more": True, "max": 1}, "messages": msgs})
        if "streams/symbol/HOT" in url:
            n = self.now
            msgs = [{"created_at": (n - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), "entities": {}}] * 30
            return FakeResp(data={"symbol": {"watchlist_count": 10}, "cursor": {"more": True, "max": 5}, "messages": msgs})
        return FakeResp(status=404)


class TestConfig(Config):
    SCANNER_DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    FINVIZ_PAGE_DELAY = 0
    TESTING = True


@pytest.fixture()
def client(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(http_client, "_session", fake)
    app = create_app(TestConfig, start_poller=False)
    finviz._cache.clear()
    c = app.test_client()
    c.get("/scanner/")
    with c.session_transaction() as s:
        token = s["_csrf"]
    c.fake = fake
    c.token = token
    yield c
    app.extensions["feed"].stop()
    app.extensions["market"].stop()


def test_parse_and_evaluate():
    rows, total = finviz.parse_screener(finviz_html([_row(1, "NVDA", "5.00%", "30.00%", "-3.00%", "120.00%", "150.00%")], 1))
    assert total == 1 and rows[0]["ticker"] == "NVDA" and rows[0]["market_cap"] == 12.34e9
    params = {"min_above_low": 30, "max_below_high": 25, "min_perf_year": 25, "strict": False}
    res = minervini.evaluate(rows[0], params)
    assert res["passed"] == res["total"] == 7


def test_screen_endpoint(client):
    r = client.post("/scanner/api/screen", headers={"X-CSRF-Token": client.token},
                    json={"filters": {"sma50": "ta_sma50_pa", "high52": "ta_highlow52w_0to25-bhx", "bogus": "x"},
                          "params": {"strict": False}})
    assert r.status_code == 200, r.data
    d = r.get_json()
    assert d["total"] == 21 and len(d["rows"]) == 21
    assert d["rows"][0]["passed"] == 7
    bad = next(x for x in d["rows"] if x["ticker"] == "BAD")
    assert bad["checks"]["above_sma50"] is False and bad["checks"]["sma_stack"] is True
    assert "ta_sma50_pa" in d["codes"] and "x" not in d["codes"]
    # strict 모드
    r2 = client.post("/scanner/api/screen", headers={"X-CSRF-Token": client.token}, json={"filters": {}, "params": {"strict": True}})
    assert all(x["passed"] == x["total"] for x in r2.get_json()["rows"])


def test_csrf_and_validation(client):
    assert client.post("/scanner/api/screen", json={}).status_code == 403
    r = client.post("/scanner/api/screen", headers={"X-CSRF-Token": client.token}, json={"custom": "ta_x'); DROP"})
    assert r.status_code == 400
    r = client.post("/scanner/api/buzz", headers={"X-CSRF-Token": client.token}, json={"symbols": ["<script>", "../etc"]})
    assert r.status_code == 400
    assert client.get("/scanner/api/buzz/history/..%2Fetc").status_code in (400, 404)


def test_buzz(client):
    r = client.post("/scanner/api/buzz", headers={"X-CSRF-Token": client.token}, json={"symbols": ["nvda", "HOT", "NONE"]})
    assert r.status_code == 200, r.data
    res = {x["symbol"]: x for x in r.get_json()["results"]}
    n = res["NVDA"]
    assert n["ok"] and n["msgs_24h"] == 6 and n["msgs_prev24h"] == 1 and n["change_pct"] == 500.0 and n["trending"]
    assert n["bull"] + n["bear"] == 6 and n["watchers"] == 900000
    assert res["HOT"]["saturated"] and res["HOT"]["change_pct"] is None
    assert not res["NONE"]["ok"]
    h = client.get("/scanner/api/buzz/history/NVDA").get_json()
    assert len(h["history"]) == 1 and h["history"][0]["msgs_24h"] == 6


def test_page_and_csrf_refresh(client):
    r = client.get("/scanner/")
    assert r.status_code == 200 and "Trend Template".encode() in r.data
    assert b'name="csrf-token"' in r.data
    # 토큰이 틀리면 403 + code=csrf (화면이 새 토큰을 받아 재시도하는 신호)
    bad = client.post("/scanner/api/screen", headers={"X-CSRF-Token": "x"}, json={})
    assert bad.status_code == 403 and bad.get_json()["code"] == "csrf"
    tok = client.get("/scanner/api/csrf").get_json()["token"]
    assert tok == client.token


def test_main_page_has_scanner_tab(client):
    html = client.get("/").get_data(as_text=True)
    assert 'href="/scanner/"' in html


def test_rate_limit_uses_forwarded_ip(client):
    lim = client.application.extensions["scanner_rate_limiter"]
    lim.per_minute = 1
    lim._hits.clear()
    h = lambda ip: {"X-Forwarded-For": ip}
    assert client.get("/scanner/api/buzz/history/NVDA", headers=h("1.1.1.1")).status_code == 200
    assert client.get("/scanner/api/buzz/history/NVDA", headers=h("1.1.1.1")).status_code == 429
    assert client.get("/scanner/api/buzz/history/NVDA", headers=h("2.2.2.2")).status_code == 200
