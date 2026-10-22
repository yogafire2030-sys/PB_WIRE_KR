"""wallstreetcn 실시간 속보 수집·번역·보관 서비스.

설계 포인트
- 단일 백그라운드 스레드가 '최근 조회된 채널'만 주기적으로 수집(불필요한 호출 차단)
- 신규 항목만 번역(스레드풀 병렬) + 번역 LRU 캐시
- 채널별 보관 건수 상한 → 메모리 사용량이 시간에 따라 증가하지 않음
- 각 항목에 단조 증가 seq 부여 → 클라이언트는 '변경분(delta)'만 받아감
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .translator import Translator

log = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"^\s*【([^】]{1,200})】\s*")
_WS_RE = re.compile(r"[ \t　]+")
MAX_TRIES = 40          # 항목당 번역 재시도 상한(백오프 포함 수 시간)
RETRY_BASE = 5          # 재시도 간격(초) — 5, 10, 20 … 최대 RETRY_MAX
RETRY_MAX = 300
BATCH_SIZE = 24         # 1회 번역 묶음(수집 주기가 밀리지 않도록 작게)


def _is_pending(it: dict, now: float) -> bool:
    return not it["translated"] and it["_tries"] < MAX_TRIES and it["_next"] <= now


def _clean(text) -> str:
    if not isinstance(text, str):
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:6000]


_CHINESE_STOCK_RE = re.compile(
    r'(?:'
    r'A股|港股|H股|科创板|创业板|北交所|上交所|深交所|港交所|'
    r'回购.{0,30}股份|集中竞价.{0,30}回购|'
    r'拟减持|股东.{0,15}减持|股东.{0,15}增持|'
    r'归母净利润|扣非净利润|前三季度净利润|半年度净利润|年度净利润|三季报|半年报|年报|业绩预告|业绩快报|'
    r'转让.{0,30}股权|收购.{0,30}股权|购买.{0,30}股权|发行股份|'
    r'控股股东|实际控制人|总股本|'
    r'撤销其他风险警示|股票复牌|股票停牌|收盘价涨幅偏离值|'
    r'^\s*ST[^\s：:]{1,10}[：:]|'
    r'\((?:60[0135]\d{3}|00[012]\d{3}|30[01]\d{3}|688\d{3}|8\d{5}|0\d{4}\.HK)\)'
    r')'
)


def _norm_text(t: str) -> str:
    return re.sub(r'[\s\W_]+', '', t or '').lower()


def _is_chinese_stock(raw: dict, stock_titles: set | None = None) -> bool:
    """중국 A주 및 홍콩 주식/개별 기업 공시 뉴스 여부 확인."""
    chs = raw.get("channels") or []
    if "a-stock-channel" in chs or "hk-stock-channel" in chs:
        return True
    themes = raw.get("related_themes") or []
    for t in themes:
        title = t.get("title", "") if isinstance(t, dict) else ""
        if any(k in title for k in ("A股", "港股", "中概股", "沪深", "北交所", "科创板", "创业板")):
            return True

    title = raw.get("title") or raw.get("title_zh") or ""
    body = raw.get("content_text") or raw.get("body_zh") or ""
    combined = f"{title} {body}"
    if _CHINESE_STOCK_RE.search(combined):
        return True

    if stock_titles:
        nt = _norm_text(title or body[:30])
        if nt and nt in stock_titles and ("：" in title or ":" in title):
            return True

    return False


def _normalize(raw: dict, stock_titles: set | None = None) -> dict | None:
    """원본 항목을 내부 구조로 변환. 형식이 이상하거나 중국/홍콩 주식 뉴스면 None."""
    if _is_chinese_stock(raw, stock_titles):
        return None
    try:
        iid = int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    title = _clean(raw.get("title"))
    body = _clean(raw.get("content_text"))
    # 본문 앞의 【제목】 중복 제거 / 제목이 비어 있으면 본문에서 추출
    m = _TITLE_RE.match(body)
    if m:
        if not title:
            title = m.group(1).strip()
        if m.group(1).strip() == title:
            body = body[m.end():]
    if not title and not body:
        return None
    try:
        ts = int(raw.get("display_time") or 0)
    except (TypeError, ValueError):
        ts = 0
    try:
        score = int(raw.get("score") or 1)
    except (TypeError, ValueError):
        score = 1
    return {
        "id": iid,
        "ts": ts,
        "important": score >= 2,
        "title_zh": title,
        "body_zh": body,
        "title_ko": "",
        "body_ko": "",
        "translated": False,
        "url": f"https://wallstreetcn.com/livenews/{iid}",
        "seq": 0,
        "_tries": 0,
        "_next": 0.0,
    }


class _ChannelStore:
    __slots__ = ("items", "seq", "last_access", "last_fetch", "error", "lock")

    def __init__(self):
        self.items: dict[int, dict] = {}
        self.seq = 0
        self.last_access = 0.0
        self.last_fetch = 0.0
        self.error = ""
        self.lock = threading.Lock()


class FeedService:
    def __init__(self, cfg):
        self.cfg = cfg
        self.channels = cfg["CHANNELS"]
        self.default = cfg["DEFAULT_CHANNEL"]
        self.stores = {k: _ChannelStore() for k in self.channels}

        self.session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "POST"}))
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (WSCN-KR-Live/1.0)",
            "Accept": "application/json",
            "Referer": "https://wallstreetcn.com/",
        })
        self.translator = Translator(cfg)
        self.pool = ThreadPoolExecutor(max_workers=cfg["TRANSLATE_WORKERS"],
                                       thread_name_prefix="tr")
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

        # 중국 A주/홍콩주식 채널 타이틀 필터 캐시
        self.stock_titles: set[str] = set()
        self._last_stock_titles_fetch = 0.0

    def _update_stock_titles(self) -> None:
        """A주 및 홍콩 주식 채널 최신 제목 목록을 수집하여 필터링 셋에 보관."""
        now = time.time()
        if now - self._last_stock_titles_fetch < 180 and self.stock_titles:
            return
        self._last_stock_titles_fetch = now
        new_set = set()
        for ch in ("a-stock-channel", "hk-stock-channel"):
            try:
                r = self.session.get(
                    self.cfg["WSCN_API_URL"],
                    params={"channel": ch, "client": "pc", "limit": 100, "accept": "live,vip-live"},
                    timeout=5,
                )
                if r.status_code == 200:
                    for it in (r.json().get("data") or {}).get("items") or []:
                        t = it.get("title") or (it.get("content_text") or "")[:30]
                        nt = _norm_text(t)
                        if nt:
                            new_set.add(nt)
            except Exception:
                pass
        if new_set:
            self.stock_titles = new_set

    # ── lifecycle ────────────────────────────────────────
    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self.stores[self.default].last_access = time.time()
            self._thread = threading.Thread(target=self._loop, name="feed-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.translator.close()
        self.session.close()

    # ── public API ───────────────────────────────────────
    def touch(self, channel: str) -> None:
        st = self.stores[channel]
        now = time.time()
        was_idle = st.last_fetch == 0 or now - st.last_access > self.cfg["CHANNEL_IDLE_SECONDS"]
        st.last_access = now
        if was_idle:
            self._wake.set()  # 새로 열린(또는 쉬던) 채널은 즉시 수집

    def delta(self, channel: str, since: int) -> dict:
        st = self.stores[channel]
        self.touch(channel)
        with st.lock:
            reset = since <= 0 or since > st.seq
            if reset:
                rows = list(st.items.values())
            else:
                rows = [it for it in st.items.values() if it["seq"] > since]
            cursor = st.seq
            err = st.error
            loaded = st.last_fetch > 0
        rows.sort(key=lambda x: (x["ts"], x["id"]), reverse=True)
        return {
            "channel": channel,
            "cursor": cursor,
            "reset": reset,
            "loaded": loaded,
            "error": err,
            "items": [{k: v for k, v in it.items() if not k.startswith("_")} for it in rows],
        }

    def status(self) -> dict:
        return {
            "engine": self.translator.engine_name,
            "last_engine": self.translator.last_engine,
            "engines": self.translator.health(),
            "cache_size": len(self.translator.cache),
            "poll_interval": self.cfg["POLL_INTERVAL"],
            "channels": {
                k: {"items": len(s.items),
                    "untranslated": sum(1 for it in s.items.values() if not it["translated"]),
                    "last_fetch": int(s.last_fetch), "error": s.error}
                for k, s in self.stores.items()
            },
        }

    # ── internals ────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            idle = self.cfg["CHANNEL_IDLE_SECONDS"]
            has_pending = False
            for key, st in self.stores.items():
                if self._stop.is_set():
                    return
                active = key == self.default or now - st.last_access < idle
                due = now - st.last_fetch >= self.cfg["POLL_INTERVAL"] - 0.5
                with st.lock:
                    untranslated = any(_is_pending(it, now) for it in st.items.values())
                if active and (due or untranslated):
                    try:
                        self._refresh(key, st)
                    except RuntimeError:  # 종료 중 번역 풀이 닫힌 경우
                        return
                    except Exception:  # 예기치 못한 오류로 수집 스레드가 죽지 않도록
                        log.exception("수집 루프 오류[%s]", key)
                    with st.lock:
                        t = time.time()
                        has_pending = has_pending or any(_is_pending(it, t) for it in st.items.values())
            wait_time = 0.5 if has_pending else 2.0
            self._wake.wait(timeout=wait_time)
            self._wake.clear()

    def _fetch(self, channel_id: str) -> list:
        r = self.session.get(
            self.cfg["WSCN_API_URL"],
            params={"channel": channel_id, "client": "pc",
                    "limit": self.cfg["FETCH_LIMIT"], "first_page": "true",
                    "accept": "live,vip-live"},
            timeout=self.cfg["HTTP_TIMEOUT"],
        )
        r.raise_for_status()
        payload = r.json()
        items = (payload.get("data") or {}).get("items") or []
        if not isinstance(items, list):
            raise ValueError("unexpected payload")
        return items

    def _fetch_history(self, channel_id: str, days: int = 3) -> list:
        """최근 3일치 속보를 역순 페이징하며 수집."""
        cutoff = time.time() - days * 86400
        all_items = []
        cursor = ""
        for _ in range(5):
            params = {
                "channel": channel_id,
                "client": "pc",
                "limit": 100,
                "accept": "live,vip-live",
            }
            if cursor:
                params["cursor"] = cursor
            else:
                params["first_page"] = "true"
            try:
                r = self.session.get(
                    self.cfg["WSCN_API_URL"],
                    params=params,
                    timeout=self.cfg["HTTP_TIMEOUT"],
                )
                r.raise_for_status()
                payload = r.json()
                items = (payload.get("data") or {}).get("items") or []
                if not items:
                    break
                all_items.extend(items)
                cursor = (payload.get("data") or {}).get("next_cursor")
                if not cursor or items[-1].get("display_time", 0) < cutoff:
                    break
            except Exception as e:
                log.warning("이력 수집 실패: %s", e)
                break
        return all_items

    def _translate_item(self, it: dict) -> dict:
        t = self.translator
        title_ko = it.get("title_ko") or (t.translate(it["title_zh"]) if it.get("title_zh") else "")
        body_ko = it.get("body_ko") or (t.translate(it["body_zh"]) if it.get("body_zh") else "")
        ok = (not it.get("title_zh") or bool(title_ko)) and (not it.get("body_zh") or bool(body_ko))
        return {"title_ko": title_ko, "body_ko": body_ko, "translated": ok}

    def _refresh(self, key: str, st: _ChannelStore) -> None:
        now = time.time()
        first = st.last_fetch == 0
        due = now - st.last_fetch >= self.cfg["POLL_INTERVAL"] - 0.5

        if first or due:
            st.last_fetch = now
            self._update_stock_titles()
            try:
                if first:
                    raw = self._fetch_history(self.channels[key]["id"], days=3)
                else:
                    raw = self._fetch(self.channels[key]["id"])
                st.error = ""
            except (requests.RequestException, ValueError) as e:
                st.error = "원본 서버 연결 실패"
                log.warning("수집 실패[%s]: %s", key, type(e).__name__)
                raw = []

            with st.lock:
                # 이미 들어와 있는 중국 A주/홍콩주식 잔여 항목 즉시 제거
                to_remove = [iid for iid, it in st.items.items() if _is_chinese_stock(it, self.stock_titles)]
                if to_remove:
                    for iid in to_remove:
                        del st.items[iid]
                    st.seq += 1

                known = st.items
                for r in raw:
                    if not isinstance(r, dict):
                        continue
                    it = _normalize(r, self.stock_titles)
                    if it and it["id"] not in known:
                        st.seq += 1
                        it["seq"] = st.seq
                        st.items[it["id"]] = it

                # 보관 상한 유지 — 오래된 항목부터 제거
                overflow = len(st.items) - self.cfg["MAX_ITEMS_PER_CHANNEL"]
                if overflow > 0:
                    for old in sorted(st.items.values(), key=lambda x: (x["ts"], x["id"]))[:overflow]:
                        del st.items[old["id"]]

        now = time.time()
        with st.lock:
            # 재시도 시각이 된 미번역 항목 (최신순 우선)
            pending = [it for it in st.items.values() if _is_pending(it, now)]
        if not pending:
            return

        pending.sort(key=lambda x: x["ts"], reverse=True)
        batch = pending[:BATCH_SIZE]

        results = list(self.pool.map(self._translate_item, batch))

        with st.lock:
            now = time.time()
            for it, res in zip(batch, results):
                if it["id"] not in st.items:  # 번역 중 상한 정리로 제거된 항목
                    continue
                changed = any(it.get(k) != v for k, v in res.items())
                it.update(res)
                it["_tries"] += 1
                if not it["translated"]:  # 지수 백오프 — 엔진 차단 중 무의미한 재요청 방지
                    it["_next"] = now + min(RETRY_MAX, RETRY_BASE * (2 ** min(it["_tries"] - 1, 8)))
                if changed:  # 내용이 바뀐 경우에만 클라이언트로 재전송
                    st.seq += 1
                    it["seq"] = st.seq
