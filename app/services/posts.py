"""'하루하나' 유튜브 영상 목록 — app/data/haruhana.json 을 읽어 검증 후 제공.

- 파일에는 유튜브 링크만 적으면 됨. 제목은 YouTube oEmbed(공식, 키 불필요)로 자동 조회
- 제목 조회는 백그라운드 스레드에서 1회만 수행·메모리 보관 → 페이지 응답 지연 없음
- 썸네일은 영상 ID로 i.ytimg.com 주소를 만들어 사용(추가 호출 없음)
- 유튜브 주소만 허용, 영상 ID 형식(11자) 검증 → 악성 링크 차단
- 파일 수정 시각이 바뀔 때만 다시 읽음(매 요청 파싱 없음)
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit

import requests

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def video_id(url: str) -> str:
    """youtu.be/ID, youtube.com/watch?v=ID, /live/ID, /shorts/ID, /embed/ID → ID (아니면 "")."""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if u.scheme not in ("https", "http") or not any(host == h or host.endswith("." + h) for h in _YT_HOSTS):
        return ""
    parts = [p for p in u.path.split("/") if p]
    if host.endswith("youtu.be"):
        vid = parts[0] if parts else ""
    elif parts[:1] == ["watch"]:
        vid = (parse_qs(u.query).get("v") or [""])[0]
    elif len(parts) >= 2 and parts[0] in ("live", "shorts", "embed", "v"):
        vid = parts[1]
    else:
        vid = ""
    return vid if _ID_RE.match(vid) else ""


def parse_posts(data) -> list[dict]:
    rows = data.get("posts") if isinstance(data, dict) else data
    out = []
    for i, p in enumerate(rows if isinstance(rows, list) else []):
        if isinstance(p, str):  # 링크만 적은 경우도 허용
            p = {"url": p}
        if not isinstance(p, dict):
            continue
        vid = video_id(str(p.get("url") or ""))
        if not vid:
            log.warning("하루하나 %d번째 항목 건너뜀(유튜브 링크 아님)", i + 1)
            continue
        date = str(p.get("date") or "").strip()
        out.append({
            "id": vid,
            "title": str(p.get("title") or "").strip()[:150],  # 비우면 유튜브 제목 자동 사용
            "date": date if _DATE_RE.match(date) else "",
            "url": f"https://www.youtube.com/watch?v={vid}",
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return out


class PostStore:
    def __init__(self, path: str, timeout: int = 5):
        self.path = path
        self.timeout = timeout
        self._mtime = None
        self._posts: list[dict] = []
        self._titles: dict[str, str] = {}   # 영상 ID → 유튜브 제목(영상 수만큼만 보관)
        self._lock = threading.Lock()
        self._fetching = False
        self._last_try = 0.0

    def all(self) -> list[dict]:
        self._reload_if_changed()
        titles = self._titles
        missing = [p["id"] for p in self._posts if not p["title"] and p["id"] not in titles]
        # 제목 없는 영상이 있으면 백그라운드 조회(실패 시 10분 뒤 재시도)
        if missing and not self._fetching and time.time() - self._last_try > 600:
            self._fetching, self._last_try = True, time.time()
            threading.Thread(target=self._fetch_titles, args=(missing,),
                             name="yt-titles", daemon=True).start()
        return [{**p, "title": p["title"] or titles.get(p["id"], "")} for p in self._posts]

    def _reload_if_changed(self) -> None:
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    self._posts = parse_posts(json.load(f))
            except (OSError, ValueError) as e:
                log.warning("하루하나 목록 읽기 실패: %s", e)
            self._mtime = mtime
            self._last_try = 0.0  # 목록이 바뀌면 새 영상 제목을 즉시 조회

    def _fetch_titles(self, ids: list[str]) -> None:
        try:
            with requests.Session() as s:
                for vid in ids:
                    try:
                        r = s.get("https://www.youtube.com/oembed",
                                  params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                                  timeout=self.timeout)
                        r.raise_for_status()
                        title = str(r.json().get("title") or "").strip()[:150]
                        if title:
                            self._titles = {**self._titles, vid: title}  # 참조 교체(락 불필요)
                    except Exception as e:
                        log.warning("유튜브 제목 조회 실패[%s]: %s", vid, type(e).__name__)
        finally:
            self._fetching = False
