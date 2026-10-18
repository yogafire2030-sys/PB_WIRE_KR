"""애플리케이션 설정 — 모든 값은 환경변수(.env)로 재정의 가능."""
import os
import secrets


def _int(name: str, default: int, lo: int, hi: int) -> int:
    """정수 환경변수를 안전 범위로 제한해 읽는다."""
    try:
        v = int(os.getenv(name, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
    JSON_AS_ASCII = False
    TEMPLATES_AUTO_RELOAD = True
    MAX_CONTENT_LENGTH = 16 * 1024  # 이 앱은 큰 요청을 받을 일이 없음

    # ── 원본 피드 ──────────────────────────────────────────
    WSCN_API_URL = os.getenv(
        "WSCN_API_URL", "https://api-one-wscn.awtmt.com/apiv1/content/lives"
    )
    POLL_INTERVAL = _int("POLL_INTERVAL", 10, 5, 300)      # 초
    FETCH_LIMIT = _int("FETCH_LIMIT", 50, 5, 100)          # 1회 수집 건수
    MAX_ITEMS_PER_CHANNEL = _int("MAX_ITEMS_PER_CHANNEL", 1000, 50, 5000)
    CHANNEL_IDLE_SECONDS = _int("CHANNEL_IDLE_SECONDS", 180, 30, 3600)
    HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 8, 2, 30)

    # ── 번역 ──────────────────────────────────────────────
    # auto | deepl | papago | google | mymemory
    TRANSLATOR = os.getenv("TRANSLATOR", "auto").strip().lower()
    DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
    PAPAGO_CLIENT_ID = os.getenv("PAPAGO_CLIENT_ID", "").strip()
    PAPAGO_CLIENT_SECRET = os.getenv("PAPAGO_CLIENT_SECRET", "").strip()
    TRANSLATE_WORKERS = _int("TRANSLATE_WORKERS", 4, 1, 16)
    TRANSLATION_CACHE_SIZE = _int("TRANSLATION_CACHE_SIZE", 3000, 100, 50000)

    CHANNELS = {
        "global": {"label": "주요뉴스", "origin": "要闻", "id": "global-channel"},
    }
    DEFAULT_CHANNEL = "global"
