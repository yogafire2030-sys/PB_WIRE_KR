"""스크리너(/scanner) — 모의(mock) Yahoo·Stocktwits 응답으로 전체 흐름 검증."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.scanner.services import http_client, minervini, universe  # noqa: E402
from config import Config  # noqa: E402


def chart(closes, name="Test Corp", price=None, volume=1_000_000):
    return {"chart": {"result": [{
        "meta": {"regularMarketPrice": price or closes[-1], "longName": name, "fullExchangeName": "NasdaqGS"},
        "indicators": {"quote": [{"close": closes, "high": [c * 1.01 for c in closes],
                                  "low": [c * 0.99 for c in closes], "volume": [volume] * len(closes)}]},
    }]}}


def uptrend(n=260, start=50.0, step=0.004):
    out, v = [], start
    for _ in range(n):
        v *= 1 + step
        out.append(round(v, 4))
    return out


def downtrend(n=260):
    return list(reversed(uptrend(n)))


class FakeResp:
    def __init__(self, status=200, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def close(self):
        pass


SERIES = {"UP": uptrend(), "FAST": uptrend(step=0.006), "DOWN": downtrend(), "CHEAP": uptrend(start=1, step=0.004)}


class FakeSession:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.calls = []

    def close(self):
        pass

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append(url)
        if "wikipedia" in url:
            return FakeResp(status=503)
        if "/v8/finance/chart/" in url:
            sym = url.rsplit("/", 1)[1]
            return FakeResp(data=chart(SERIES[sym], f"{sym} Corp")) if sym in SERIES else FakeResp(status=404)
        if "trending" in url:
            return FakeResp(data={"symbols": [{"symbol": "UP"}]})
        if "streams/symbol/UP" in url:
            n = self.now
            msgs = [{"created_at": (n - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "entities": {"sentiment": {"basic": "Bullish" if h % 2 else "Bearish"}}} for h in
                    [1, 2, 3, 4, 5, 6, 30, 50]]
            return FakeResp(data={"symbol": {"watchlist_count": 900000, "title": "UP"},
                                  "cursor": {"more": True, "max": 1}, "messages": msgs})
        return FakeResp(status=404)


class TestConfig(Config):
    SCANNER_DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
    TESTING = True


@pytest.fixture()
def client(monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr(http_client, "_session", fake)
    monkeypatch.setitem(universe.BUNDLED, "sp500", ["UP", "DOWN", "CHEAP", "GONE"])
    monkeypatch.setitem(universe.BUNDLED, "ndx", ["UP", "FAST"])
    app = create_app(TestConfig, start_poller=False)
    eng = app.extensions["scan_engine"]
    eng.ensure_fresh = lambda: None  # 테스트에서는 계산을 직접 실행
    c = app.test_client()
    c.get("/scanner/")
    with c.session_transaction() as s:
        c.token = s["_csrf"]
    c.fake, c.eng = fake, eng
    yield c
    app.extensions["feed"].stop()
    app.extensions["market"].stop()


def post(c, url, body):
    return c.post(url, headers={"X-CSRF-Token": c.token}, json=body)


PARAMS = {"min_above_low": 30, "max_below_high": 25, "min_rs": 70, "strict": False}


def test_compute_uptrend_passes_all_eight():
    r = minervini.compute("UP", chart(uptrend()))
    assert r["sma50"] > r["sma150"] > r["sma200"] > r["sma200_1m"]
    assert -1.5 < r["from_high"] <= 0 and r["from_low"] > 100
    r["rs_rank"] = 90
    res = minervini.evaluate(r, PARAMS)
    assert res["passed"] == res["total"] == 8


def test_compute_downtrend_and_short_history():
    r = minervini.compute("DOWN", chart(downtrend()))
    r["rs_rank"] = 5
    res = minervini.evaluate(r, PARAMS)
    assert res["checks"]["above_sma50"] is False and res["checks"]["sma200_rising"] is False
    assert minervini.compute("NEW", chart(uptrend(40))) is None      # 60일 미만 → 제외
    short = minervini.compute("YOUNG", chart(uptrend(120)))         # 200일선 없음 → 판정 불가(None)
    assert short["sma200"] is None and minervini.evaluate({**short, "rs_rank": 50}, PARAMS)["checks"]["sma150_gt_200"] is None


def test_rs_rank_percentile():
    rows = [{"rs_raw": v, "rs_rank": None} for v in (10, 20, 20, 40)]
    minervini.rank_rs(rows)
    assert [r["rs_rank"] for r in rows] == [1, 50, 50, 99]


def test_wiki_parser_and_normalize():
    html = ("<table id='constituents'><tr><th>Symbol</th><th>Security</th></tr>"
            + "".join(f"<tr><td>{t}</td><td>x</td></tr>" for t in ("AA", "BB", "CC", "DD", "EE"))
            + "<tr><td>BRK.B</td><td>x</td></tr><tr><td>&lt;script&gt;</td><td>x</td></tr></table>")
    got = universe.parse_wiki_table(html, min_count=3)
    assert "BRK-B" in got and len(got) == 6
    assert universe.parse_wiki_table(html, min_count=100) == []    # 너무 적으면 잘못 읽은 표로 간주


def test_bundled_lists_are_valid():
    import importlib
    u = importlib.reload(universe)
    assert len(u.BUNDLED["sp500"]) >= 480 and len(u.BUNDLED["ndx"]) >= 95
    assert all(u.normalize(t) == t for t in u.BUNDLED["sp500"] + u.BUNDLED["ndx"])


def test_screen_preparing_then_results(client):
    r = post(client, "/scanner/api/screen", {"params": {}})
    assert r.status_code == 202 and r.get_json()["preparing"]
    client.eng._run()  # 동기 실행
    st = client.get("/scanner/api/status").get_json()
    assert st["ready"] and st["count"] == 4 and st["failed"] == 1 and st["source"] == "bundled"

    d = post(client, "/scanner/api/screen", {"params": {"min_price": "10"}}).get_json()
    tickers = [x["ticker"] for x in d["rows"]]
    assert "CHEAP" not in tickers                        # 주가 $10 이하 제외
    assert tickers[0] in ("UP", "FAST") and d["rows"][0]["passed"] == 8
    assert d["full"] >= 1 and d["total"] == 3

    nd = post(client, "/scanner/api/screen", {"params": {"universe": "ndx", "min_price": 0}}).get_json()
    assert sorted(x["ticker"] for x in nd["rows"]) == ["FAST", "UP"]
    strict = post(client, "/scanner/api/screen", {"params": {"strict": True, "min_price": 0}}).get_json()
    assert all(x["passed"] == x["total"] for x in strict["rows"])


def test_sanitize_rejects_unknown_values(client):
    from app.scanner.filters import sanitize
    p = sanitize({"universe": "<x>", "min_price": "999", "min_rs": "abc", "max_below_high": 500, "strict": "yes"})
    assert p["universe"] == "all" and p["min_price"] == 10 and p["min_rs"] == 70
    assert p["max_below_high"] == 100 and p["strict"] is False


def test_buzz_and_history(client):
    r = post(client, "/scanner/api/buzz", {"symbols": ["up", "NONE"]})
    assert r.status_code == 200, r.data
    res = {x["symbol"]: x for x in r.get_json()["results"]}
    u = res["UP"]
    assert u["ok"] and u["msgs_24h"] == 6 and u["msgs_prev24h"] == 1 and u["trending"]
    assert not res["NONE"]["ok"]
    h = client.get("/scanner/api/buzz/history/UP").get_json()
    assert len(h["history"]) == 1 and h["history"][0]["msgs_24h"] == 6


def test_csrf_and_validation(client):
    bad = client.post("/scanner/api/screen", headers={"X-CSRF-Token": "x"}, json={})
    assert bad.status_code == 403 and bad.get_json()["code"] == "csrf"
    assert client.get("/scanner/api/csrf").get_json()["token"] == client.token
    assert post(client, "/scanner/api/buzz", {"symbols": ["<script>", "../etc"]}).status_code == 400
    assert client.get("/scanner/api/buzz/history/..%2Fetc").status_code in (400, 404)


def test_main_page_has_scanner_tab(client):
    assert 'href="/scanner/"' in client.get("/").get_data(as_text=True)


def test_rate_limit_uses_forwarded_ip(client):
    lim = client.application.extensions["scanner_rate_limiter"]
    lim.per_minute = 1
    lim._hits.clear()
    h = lambda ip: {"X-Forwarded-For": ip}  # noqa: E731
    assert client.get("/scanner/api/buzz/history/UP", headers=h("1.1.1.1")).status_code == 200
    assert client.get("/scanner/api/buzz/history/UP", headers=h("1.1.1.1")).status_code == 429
    assert client.get("/scanner/api/buzz/history/UP", headers=h("2.2.2.2")).status_code == 200
