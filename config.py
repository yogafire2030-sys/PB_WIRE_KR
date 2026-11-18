"""애플리케이션 설정 — 모든 값은 환경변수(.env)로 재정의 가능."""
import os
import re
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
    RETENTION_HOURS = _int("RETENTION_HOURS", 48, 1, 168)  # 이 시간보다 오래된 속보는 제외

    # ── 스크리너(/scanner) ─────────────────────────────────
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"
    SCANNER_DB_PATH = os.getenv("SCANNER_DB_PATH",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), "instance", "buzz.db"))
    SCANNER_CONNECT_TIMEOUT = 5
    SCANNER_READ_TIMEOUT = 20
    SCAN_REFRESH_MIN = _int("SCAN_REFRESH_MIN", 60, 10, 1440)       # 전 종목 재계산 주기(분)
    SCAN_WORKERS = _int("SCAN_WORKERS", 6, 1, 12)                    # Yahoo 동시 조회 수
    STOCKTWITS_CACHE_TTL = _int("STOCKTWITS_CACHE_TTL", 600, 0, 86400)
    STOCKTWITS_MAX_PAGES = _int("STOCKTWITS_MAX_PAGES", 4, 1, 10)
    STOCKTWITS_MAX_WORKERS = _int("STOCKTWITS_MAX_WORKERS", 4, 1, 8)
    STOCKTWITS_MAX_SYMBOLS = 30
    SCANNER_RATE_LIMIT_PER_MIN = _int("SCANNER_RATE_LIMIT_PER_MIN", 20, 1, 1000)

    # ── 유튜브 라이브 ──────────────────────────────────────
    # 영상 ID(11자). 형식이 틀리면 라이브 영역을 표시하지 않음(빈 값 = 끄기)
    _yt = os.getenv("YOUTUBE_LIVE_ID", "VlaCnp1fxI8").strip()
    YOUTUBE_LIVE_ID = _yt if re.fullmatch(r"[A-Za-z0-9_-]{11}", _yt) else ""
    del _yt
    HTTP_TIMEOUT = _int("HTTP_TIMEOUT", 8, 2, 30)

    # ── 번역 ──────────────────────────────────────────────
    # auto | deepl | azure | papago | edge | google | bing | mymemory
    # (지정한 엔진이 맨 앞, 나머지는 자동 폴백)
    TRANSLATOR = os.getenv("TRANSLATOR", "auto").strip().lower()
    DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "").strip()
    AZURE_TRANSLATOR_KEY = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
    AZURE_TRANSLATOR_REGION = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
    MYMEMORY_EMAIL = os.getenv("MYMEMORY_EMAIL", "").strip()
    PAPAGO_CLIENT_ID = os.getenv("PAPAGO_CLIENT_ID", "").strip()
    PAPAGO_CLIENT_SECRET = os.getenv("PAPAGO_CLIENT_SECRET", "").strip()
    TRANSLATE_WORKERS = _int("TRANSLATE_WORKERS", 4, 1, 16)
    TRANSLATION_CACHE_SIZE = _int("TRANSLATION_CACHE_SIZE", 3000, 100, 50000)

    # ── 주요 지수 ──────────────────────────────────────────
    MARKET_POLL_INTERVAL = _int("MARKET_POLL_INTERVAL", 15, 5, 300)  # 초

    CHANNELS = {
        "global": {"label": "주요뉴스", "origin": "要闻", "id": "global-channel"},
    }
    DEFAULT_CHANNEL = "global"
