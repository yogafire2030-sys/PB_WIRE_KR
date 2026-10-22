import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from app.services.translator import Translator, _LRU, is_valid_korean, split_chunks

NETWORK = os.getenv("RUN_NETWORK_TESTS") == "1"


def _cfg(**kw):
    cfg = {k: v for k, v in Config.__dict__.items() if k.isupper()}
    cfg.update(kw)
    return cfg


def test_lru_cache():
    lru = _LRU(maxsize=2)
    lru.put("a", "1")
    lru.put("b", "2")
    assert lru.get("a") == "1"
    lru.put("c", "3")  # 'b' should be evicted because 'a' was accessed
    assert lru.get("b") is None
    assert lru.get("a") == "1"
    assert lru.get("c") == "3"


def test_validation_rejects_untranslated_output():
    src = "美联储宣布维持利率不变"
    assert not is_valid_korean(src, src)                       # 원문 그대로 반환
    assert not is_valid_korean(src, "")
    assert not is_valid_korean(src, "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS")
    assert not is_valid_korean(src, "<html><title>Sorry...</title></html>")
    assert is_valid_korean(src, "연준, 금리 동결 발표")
    assert is_valid_korean("123.45", "123.45")


def test_split_chunks_respects_limit_and_content():
    text = ("美联储宣布维持利率不变。" * 60) + "\n" + ("市场预期年内降息两次！" * 40)
    chunks = split_chunks(text, 450)
    assert len(chunks) > 1
    assert all(len(c) <= 450 for c in chunks)
    assert "".join(chunks) == text
    long_sentence = "无标点" * 400
    assert all(len(c) <= 450 for c in split_chunks(long_sentence, 450))


def test_fallback_skips_bad_engine_and_cooldown():
    t = Translator(_cfg(TRANSLATOR="auto"))
    calls = {"bad": 0, "good": 0}

    def bad(text):
        calls["bad"] += 1
        return text  # 원문 그대로 — 무효 결과

    def good(text):
        calls["good"] += 1
        return "번역됨"

    try:
        t.engines[0].fn = bad
        t.engines[1].fn = good
        for e in t.engines[2:]:
            e.until = time.time() + 999
        assert t.translate("你好") == "번역됨"
        assert t.translate("世界") == "번역됨"
        assert calls["bad"] == 1          # 실패 후 쿨다운 → 두 번째는 건너뜀
        assert t.translate("你好") == "번역됨"
        assert calls["good"] == 2         # 캐시 적중
    finally:
        t.close()


def test_all_engines_fail_returns_empty_and_not_cached():
    t = Translator(_cfg())
    try:
        for e in t.engines:
            e.fn = lambda text: (_ for _ in ()).throw(ValueError("blocked"))
        assert t.translate("你好") == ""
        assert len(t.cache) == 0
    finally:
        t.close()


def test_long_text_is_chunked_and_joined():
    t = Translator(_cfg())
    seen = []

    def fake(text):
        seen.append(len(text))
        return "번역"

    try:
        t.engines[0].fn = fake
        out = t.translate("美联储宣布维持利率不变。" * 100)
        assert out and "번역" in out
        assert max(seen) <= 450 and len(seen) > 1
    finally:
        t.close()


def test_no_chinese_is_returned_as_is():
    t = Translator(_cfg())
    try:
        assert t.translate("") == ""
        assert t.translate("   ") == ""
        assert t.translate("S&P 500 +1.2%") == "S&P 500 +1.2%"
    finally:
        t.close()


@pytest.mark.skipif(not NETWORK, reason="RUN_NETWORK_TESTS=1 일 때만 실제 엔진 호출")
def test_translator_live():
    t = Translator(_cfg())
    try:
        result = t.translate("你好")
        assert "안녕" in result
    finally:
        t.close()
