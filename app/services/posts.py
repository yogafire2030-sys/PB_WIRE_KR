"""'하루하나' 글 목록 — app/data/haruhana.json 을 읽어 검증 후 제공.

- 서버 시작 시 1회 로드 + 파일 수정 시각이 바뀌면 다시 읽음(매 요청 파싱 없음)
- url은 https + Notion 도메인만 허용(javascript: 등 악성 링크 차단), 글자 수 상한 적용
- 형식이 틀린 항목은 건너뛰고 경고 로그만 남김(사이트는 계속 동작)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

_ALLOWED_HOSTS = ("notion.so", "notion.site", "notion.com")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_url(url: str) -> bool:
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


def parse_posts(data) -> list[dict]:
    rows = data.get("posts") if isinstance(data, dict) else data
    out = []
    for i, p in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(p, dict):
            continue
        url = str(p.get("url") or "").strip()
        title = str(p.get("title") or "").strip()[:120]
        if not title or not _valid_url(url):
            log.warning("하루하나 %d번째 항목 건너뜀(제목 없음 또는 Notion 주소 아님)", i + 1)
            continue
        date = str(p.get("date") or "").strip()
        out.append({
            "title": title,
            "url": url[:500],
            "date": date if _DATE_RE.match(date) else "",
            "summary": str(p.get("summary") or "").strip()[:300],
        })
    return out


class PostStore:
    def __init__(self, path: str):
        self.path = path
        self._mtime = None
        self._posts: list[dict] = []
        self._lock = threading.Lock()

    def all(self) -> list[dict]:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return []
        if mtime != self._mtime:
            with self._lock:
                if mtime != self._mtime:
                    try:
                        with open(self.path, encoding="utf-8") as f:
                            self._posts = parse_posts(json.load(f))
                    except (OSError, ValueError) as e:
                        log.warning("하루하나 목록 읽기 실패: %s", e)
                    self._mtime = mtime
        return self._posts
