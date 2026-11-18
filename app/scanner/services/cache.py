"""스레드 안전한 TTL + LRU 캐시.

최대 항목 수가 고정되어 있어 장시간 실행해도 메모리가 계속 늘어나지 않습니다.
"""
import threading
import time
from collections import OrderedDict
from typing import Any, Hashable, Optional


class TTLCache:
    __slots__ = ("_data", "_lock", "_maxsize", "_ttl")

    def __init__(self, maxsize: int = 256, ttl: float = 600.0):
        self._data: "OrderedDict[Hashable, tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._maxsize = max(1, int(maxsize))
        self._ttl = float(ttl)

    def get(self, key: Hashable) -> Optional[Any]:
        if self._ttl <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < now:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._data[key] = (time.monotonic() + self._ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
