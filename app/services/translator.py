"""중국어 → 한국어 번역 엔진 (DeepL / Papago / Google / MyMemory).

- LRU 캐시(크기 제한)로 동일 문장 재번역을 막아 비용·지연·메모리를 함께 관리
- IPv4 우선 연결(AF_INET)을 통해 듀얼 스택 환경에서 발생하는 Google 429 차단(CAPTCHA) 방지
- WSCN 수집 세션과 분리된 전용 세션 및 일반 브라우저 헤더 사용
- 엔진 실패 시 다음 엔진/엔드포인트로 자동 폴백, 모두 실패하면 원문 유지
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import threading
import time
from collections import OrderedDict

import requests
from requests.adapters import HTTPAdapter
import urllib3.util.connection as urllib3_conn

# Google Translate 및 일부 번역 API는 듀얼 스택(IPv4/IPv6) 환경에서 IPv6 요청 시 429 차단(CAPTCHA)이 발생하므로
# IPv4 우선 연결(AF_INET)을 적용합니다.
try:
    urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:
    pass

log = logging.getLogger(__name__)

MAX_CHARS = 4500  # 엔진별 요청 상한보다 보수적으로


class _LRU:
    """스레드 안전 LRU — maxsize 초과 시 가장 오래된 항목 제거(메모리 상한 보장)."""

    def __init__(self, maxsize: int):
        self._d: OrderedDict[str, str] = OrderedDict()
        self._max = maxsize
        self._lock = threading.Lock()

    def get(self, k: str):
        with self._lock:
            v = self._d.get(k)
            if v is not None:
                self._d.move_to_end(k)
            return v

    def put(self, k: str, v: str) -> None:
        with self._lock:
            self._d[k] = v
            self._d.move_to_end(k)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def __len__(self):
        return len(self._d)


class Translator:
    def __init__(self, cfg, session: requests.Session | None = None):
        self.timeout = cfg.get("HTTP_TIMEOUT", 8)
        self.cache = _LRU(cfg.get("TRANSLATION_CACHE_SIZE", 3000))
        self.deepl_key = cfg.get("DEEPL_API_KEY", "")
        self.papago_id = cfg.get("PAPAGO_CLIENT_ID", "")
        self.papago_secret = cfg.get("PAPAGO_CLIENT_SECRET", "")

        # Bing 토큰 캐시 및 락
        self._bing_lock = threading.Lock()
        self._bing_key = None
        self._bing_token = None
        self._bing_ig = None
        self._bing_iid = None
        self._bing_token_time = 0.0

        # 번역 전용 세션: WSCN 수집용 세션의 Referer나 비표준 User-Agent 오염 방지
        self._owns_session = session is None
        if session is not None:
            self.s = session
        else:
            self.s = requests.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=1)
            self.s.mount("https://", adapter)
            self.s.mount("http://", adapter)

        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self.s.headers.pop("Referer", None)

        order = {
            "deepl": ["deepl"],
            "papago": ["papago"],
            "bing": ["bing"],
            "google": ["google"],
            "mymemory": ["mymemory"],
        }.get(cfg.get("TRANSLATOR", "auto"), ["deepl", "papago", "bing", "google", "mymemory"])

        engines = []
        for name in order:
            if name == "deepl" and self.deepl_key:
                engines.append(("DeepL", self._deepl))
            elif name == "papago" and self.papago_id and self.papago_secret:
                engines.append(("Papago", self._papago))
            elif name == "bing":
                engines.append(("Bing", self._bing))
            elif name == "google":
                engines.append(("Google", self._google))
            elif name == "mymemory":
                engines.append(("MyMemory", self._mymemory))
        if not engines:  # 기본 폴백 구성
            engines.append(("Bing", self._bing))
            engines.append(("Google", self._google))
            engines.append(("MyMemory", self._mymemory))
        self.engines = engines
        self.engine_name = engines[0][0]

    # ── public ───────────────────────────────────────────
    def translate(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        text = text[:MAX_CHARS]
        key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        for name, fn in self.engines:
            try:
                out = fn(text)
                if out:
                    self.cache.put(key, out)
                    return out
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
                log.warning("번역 실패(%s): %s", name, type(e).__name__)
        return ""  # 실패 — 호출 측에서 원문 표시

    def close(self) -> None:
        if self._owns_session and hasattr(self, "s"):
            self.s.close()

    # ── engines ──────────────────────────────────────────
    def _deepl(self, text: str) -> str:
        host = "api-free.deepl.com" if self.deepl_key.endswith(":fx") else "api.deepl.com"
        r = self.s.post(
            f"https://{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {self.deepl_key}"},
            data={"text": text, "source_lang": "ZH", "target_lang": "KO"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["translations"][0]["text"].strip()

    def _papago(self, text: str) -> str:
        r = self.s.post(
            "https://papago.apigw.ntruss.com/nmt/v1/translation",
            headers={
                "X-NCP-APIGW-API-KEY-ID": self.papago_id,
                "X-NCP-APIGW-API-KEY": self.papago_secret,
            },
            data={"source": "zh-CN", "target": "ko", "text": text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["result"]["translatedText"].strip()

    def _get_bing_tokens(self, force: bool = False):
        now = time.time()
        if not force and self._bing_token and (now - self._bing_token_time < 1800):
            return self._bing_key, self._bing_token, self._bing_ig, self._bing_iid

        with self._bing_lock:
            if not force and self._bing_token and (time.time() - self._bing_token_time < 1800):
                return self._bing_key, self._bing_token, self._bing_ig, self._bing_iid

            r = self.s.get("https://www.bing.com/translator", timeout=self.timeout)
            r.raise_for_status()

            ig_m = re.search(r'IG:"([A-Za-z0-9]+)"', r.text)
            iid_m = re.search(r'data-iid="([^"]+)"', r.text)
            abuse_m = re.search(r'params_AbusePreventionHelper\s*=\s*(\[[^;]+\]);', r.text)

            if not (ig_m and abuse_m):
                raise ValueError("Could not extract Bing tokens from page")

            abuse = json.loads(abuse_m.group(1))
            self._bing_key = abuse[0]
            self._bing_token = abuse[1]
            self._bing_ig = ig_m.group(1)
            self._bing_iid = iid_m.group(1) if iid_m else "translator.5028"
            self._bing_token_time = time.time()
            return self._bing_key, self._bing_token, self._bing_ig, self._bing_iid

    def _bing(self, text: str) -> str:
        for attempt in range(2):
            try:
                key, token, ig, iid = self._get_bing_tokens(force=(attempt > 0))
                url = f"https://www.bing.com/ttranslatev3?isVertical=1&IG={ig}&IID={iid}"
                data = {
                    "fromLang": "zh-Hans",
                    "to": "ko",
                    "text": text,
                    "token": token,
                    "key": key,
                }
                r = self.s.post(url, data=data, timeout=self.timeout)
                if r.status_code == 200:
                    res_json = r.json()
                    if isinstance(res_json, list) and res_json:
                        translations = res_json[0].get("translations") or []
                        if translations and "text" in translations[0]:
                            out = translations[0]["text"].strip()
                            if out:
                                return out
                    elif isinstance(res_json, dict) and res_json.get("statusCode") == 205:
                        self._bing_token = None
                        continue
                r.raise_for_status()
            except Exception:
                self._bing_token = None
                if attempt == 1:
                    raise
                time.sleep(0.5)
        return ""

    def _google(self, text: str) -> str:
        # 429 일시 차단 감지 시 30초간 무의미한 네트워크 재요청 방지
        if time.time() < getattr(self, "_google_blocked_until", 0):
            return ""
        timeout = min(self.timeout, 3)
        try:
            r = self.s.get(
                "https://translate.google.com/translate_a/single",
                params={"client": "at", "sl": "zh-CN", "tl": "ko", "dt": "t", "q": text},
                timeout=timeout,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list) and data and isinstance(data[0], list):
                    out = "".join(
                        seg[0] for seg in data[0]
                        if seg and isinstance(seg, (list, tuple)) and isinstance(seg[0], str)
                    ).strip()
                    if out:
                        return out
            elif r.status_code == 429:
                self._google_blocked_until = time.time() + 30
        except Exception:
            pass
        return ""



    def _mymemory(self, text: str) -> str:
        r = self.s.get(
            "https://api.mymemory.translated.net/get",
            params={"q": text, "langpair": "zh-CN|ko"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("responseStatus") == 200:
            res = (data.get("responseData") or {}).get("translatedText", "").strip()
            if res:
                return res
        raise ValueError(f"MyMemory error: {data.get('responseDetails')}")

