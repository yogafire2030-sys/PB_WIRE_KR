"""Finviz·Stocktwits 호출용 공유 HTTP 세션.

- 커넥션 풀 재사용으로 TCP/TLS 핸드셰이크 비용 절감
- 429/5xx에 대해 제한적 재시도(지수 백오프)
- 앱 종료 시 세션을 닫아 소켓 누수 방지
"""
import atexit
import threading

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)


def describe_error(exc: Exception) -> str:
    """네트워크 예외를 사용자용 메시지로 변환 (내부 상세는 노출하지 않음)."""
    if isinstance(exc, requests.exceptions.Timeout):
        return "응답 시간 초과 — 잠시 후 다시 시도하세요."
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "연결 실패 — 잠시 후 다시 시도하세요."
    return f"연결 실패 ({type(exc).__name__})"


_session = None
_lock = threading.Lock()


def get_session() -> requests.Session:
    global _session
    if _session is None:
        with _lock:
            if _session is None:
                s = requests.Session()
                retry = Retry(
                    total=2,
                    backoff_factor=0.8,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset({"GET"}),
                    respect_retry_after_header=True,
                )
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=10, max_retries=retry)
                s.mount("https://", adapter)
                s.headers.update({
                    "User-Agent": _USER_AGENT,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                })
                _session = s
    return _session


def _close_session() -> None:
    global _session
    if _session is not None:
        _session.close()
        _session = None


atexit.register(_close_session)
