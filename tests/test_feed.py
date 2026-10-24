import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from app.services.feed import FeedService


def _raw(i, text):
    return {"id": i, "title": "", "content_text": text, "display_time": int(time.time()) - 60 + i, "score": 1}


def test_failed_translation_backs_off_then_recovers():
    cfg = {k: v for k, v in Config.__dict__.items() if k.isupper()}
    svc = FeedService(cfg)
    try:
        svc._update_stock_titles = lambda: None
        svc._fetch_history = lambda ch, hours=48: [_raw(1, "美联储宣布维持利率不变。")]
        svc._fetch = lambda ch: []
        st = svc.stores["global"]

        svc.translator.translate = lambda text: ""          # 모든 엔진 차단 상황
        svc._refresh("global", st)
        it = st.items[1]
        assert it["translated"] is False and it["_tries"] == 1
        assert it["_next"] > 0                               # 백오프 예약
        seq_before = st.seq

        svc._refresh("global", st)                           # 백오프 중 → 재시도 안 함
        assert it["_tries"] == 1 and st.seq == seq_before

        it["_next"] = 0
        svc.translator.translate = lambda text: "연준, 금리 동결 발표."
        svc._refresh("global", st)
        assert it["translated"] is True and it["body_ko"].startswith("연준")
        assert st.seq > seq_before                           # 클라이언트로 갱신 전달

        d = svc.delta("global", seq_before)
        assert [x["id"] for x in d["items"]] == [1]
        assert "_next" not in d["items"][0]
    finally:
        svc.stop()


def test_items_older_than_retention_are_dropped():
    cfg = {k: v for k, v in Config.__dict__.items() if k.isupper()}
    svc = FeedService(cfg)
    try:
        svc._update_stock_titles = lambda: None
        old = _raw(1, "旧闻。")
        old["display_time"] = int(time.time()) - 49 * 3600
        svc._fetch_history = lambda ch, hours=48: [old, _raw(2, "新闻。")]
        svc.translator.translate = lambda text: "번역"
        st = svc.stores["global"]
        svc._refresh("global", st)
        assert list(st.items) == [2]
    finally:
        svc.stop()
