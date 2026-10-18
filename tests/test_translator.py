import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from app.services.translator import Translator, _LRU


def test_lru_cache():
    lru = _LRU(maxsize=2)
    lru.put("a", "1")
    lru.put("b", "2")
    assert lru.get("a") == "1"
    lru.put("c", "3")  # 'b' should be evicted because 'a' was accessed
    assert lru.get("b") is None
    assert lru.get("a") == "1"
    assert lru.get("c") == "3"


def test_translator_basic():
    t = Translator(Config.__dict__)
    try:
        # Empty text
        assert t.translate("") == ""
        assert t.translate("   ") == ""

        # Test translation
        result = t.translate("你好")
        assert len(result) > 0
        assert "안녕" in result

        # Test cache hit
        cached = t.translate("你好")
        assert cached == result
    finally:
        t.close()


def test_translator_fallback():
    # Test MyMemory fallback specifically
    cfg = dict(Config.__dict__)
    cfg["TRANSLATOR"] = "mymemory"
    t = Translator(cfg)
    try:
        res = t.translate("中国")
        assert len(res) > 0
        assert "중국" in res
    finally:
        t.close()
