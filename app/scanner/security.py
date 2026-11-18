"""스크리너 API 보안: CSRF 토큰, IP 기반 요청 제한."""
import hmac
import secrets
import threading
import time
from collections import OrderedDict, deque
from functools import wraps

from flask import abort, current_app, jsonify, request, session

CSRF_HEADER = "X-CSRF-Token"


def get_csrf_token() -> str:
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def csrf_protect(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        sent = request.headers.get(CSRF_HEADER, "")
        expected = session.get("_csrf", "")
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


class RateLimiter:
    """슬라이딩 윈도우 제한. 추적 IP 수 상한으로 메모리 사용량 고정."""

    def __init__(self, per_minute: int, max_clients: int = 2048):
        self.per_minute = per_minute
        self.max_clients = max_clients
        self._hits: "OrderedDict[str, deque]" = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                q = deque(maxlen=self.per_minute)
                self._hits[key] = q
                while len(self._hits) > self.max_clients:
                    self._hits.popitem(last=False)
            self._hits.move_to_end(key)
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def rate_limited(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        limiter = current_app.extensions["scanner_rate_limiter"]
        # Render 등 프록시 뒤에서는 ProxyFix가 remote_addr를 실제 접속자 IP로 바꿔 줌
        if not limiter.allow(request.remote_addr or "unknown"):
            return jsonify(error="요청이 너무 많습니다. 1분 후 다시 시도하세요."), 429
        return fn(*args, **kwargs)
    return wrapper
