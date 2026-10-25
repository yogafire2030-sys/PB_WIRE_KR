"""중국어 → 한국어 번역 엔진.

엔진 우선순위(auto): DeepL → Azure → Papago → Edge → Google → Bing → MyMemory
  - DeepL / Azure / Papago : API 키가 있을 때만 사용(가장 안정적)
  - Edge / Google / Bing / MyMemory : 키 없이 쓰는 무료 엔드포인트

원문이 그대로 노출되던 문제의 대응
  1) 결과 검증 : 한글이 없거나 한자가 그대로 남은 "가짜 성공"(차단 페이지, 원문 반환,
     MyMemory 쿼터 경고문 등)은 실패로 처리하고 캐시에 넣지 않는다.
  2) 문장 단위 분할 : 긴 본문은 450자 이하 조각으로 나눠 번역(MyMemory 500자 제한,
     Google GET URL 길이 초과 문제 해소). 조각 단위로 캐시해 재시도 비용 최소화.
  3) 서킷 브레이커 : 실패한 엔진은 지수적으로 늘어나는 시간 동안 건너뛴다.
     클라우드 IP 차단(429/CAPTCHA) 상태에서 타임아웃을 반복하며 수 분씩
     번역 큐가 멈추던 문제 해소.
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

# 듀얼 스택 환경에서 IPv6 요청이 Google 429(CAPTCHA)로 차단되는 문제 회피 — IPv4 우선
try:
    urllib3_conn.allowed_gai_family = lambda: socket.AF_INET
except Exception:  # pragma: no cover
    pass

log = logging.getLogger(__name__)

MAX_CHARS = 6000   # 항목 1건 최대 길이(수집 단계 상한과 동일)
# DeepL `context` — 번역 대상이 아닌 배경 정보(과금 제외). 용어·문체 선택 품질을 높임
BASE_CONTEXT = ("중국 금융 매체 华尔街见闻(월스트리트견문)의 7×24 실시간 금융·경제 속보. "
                "한국 증권사 PB가 읽는 뉴스로, 간결한 한국어 기사체로 번역. "
                "美联储=연준, 央行=중앙은행, 国债=국채, 降息=금리 인하, 加息=금리 인상, "
                "港股=홍콩 증시, A股=중국 본토 증시, 美股=미국 증시, 基点=bp, 同比=전년 대비, 环比=전월 대비.")
CHUNK_CHARS = 450  # 모든 엔진의 1회 요청 한도보다 작게(MyMemory 500자)

_HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_HANGUL_RE = re.compile(r"[가-힣]")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？；!?;\n])")
_BAD_MARKERS = ("MYMEMORY WARNING", "QUERY LENGTH LIMIT", "INVALID LANGUAGE PAIR",
                "<HTML", "<!DOCTYPE")


def is_valid_korean(src: str, out: str) -> bool:
    """번역 결과가 실제 한국어 번역인지 검증."""
    if not out:
        return False
    head = out[:200].upper()
    if any(m in head for m in _BAD_MARKERS):
        return False
    src_han = len(_HAN_RE.findall(src))
    if src_han == 0:
        return True
    out_han = len(_HAN_RE.findall(out))
    hangul = len(_HANGUL_RE.findall(out))
    # 고유명사 등 일부 한자 잔존은 허용, 원문 수준으로 남아 있으면 실패
    return hangul > 0 and out_han <= max(3, src_han * 0.2)


def split_chunks(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """문장 경계(。！？；\\n) 기준으로 limit 이하 조각으로 분할."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for part in _SENT_SPLIT_RE.split(text):
        if not part:
            continue
        while len(part) > limit:  # 문장 하나가 너무 길면 강제 분할
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(part[:limit])
            part = part[limit:]
        if len(buf) + len(part) > limit:
            chunks.append(buf)
            buf = part
        else:
            buf += part
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


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


class _Engine:
    """엔진 + 서킷 브레이커 상태."""
    __slots__ = ("name", "fn", "premium", "fails", "until", "ok", "ng")

    BASE_COOLDOWN = 15
    MAX_COOLDOWN = 600

    def __init__(self, name, fn, premium=False):
        self.name, self.fn, self.premium = name, fn, premium
        self.fails = 0
        self.until = 0.0
        self.ok = 0
        self.ng = 0

    def available(self, now: float) -> bool:
        return now >= self.until

    def success(self) -> None:
        self.fails = 0
        self.until = 0.0
        self.ok += 1

    def failure(self, cooldown: float | None = None) -> None:
        self.fails += 1
        self.ng += 1
        cool = cooldown or min(self.MAX_COOLDOWN, self.BASE_COOLDOWN * (2 ** min(self.fails - 1, 6)))
        self.until = time.time() + cool


class Translator:
    AUTO_ORDER = ("deepl", "azure", "papago", "edge", "google", "bing", "mymemory")

    def __init__(self, cfg, session: requests.Session | None = None):
        self.timeout = cfg.get("HTTP_TIMEOUT", 8)
        self.free_timeout = min(self.timeout, 5)
        self.cache = _LRU(cfg.get("TRANSLATION_CACHE_SIZE", 3000))
        self.deepl_key = cfg.get("DEEPL_API_KEY", "")
        self.azure_key = cfg.get("AZURE_TRANSLATOR_KEY", "")
        self.azure_region = cfg.get("AZURE_TRANSLATOR_REGION", "")
        self.papago_id = cfg.get("PAPAGO_CLIENT_ID", "")
        self.papago_secret = cfg.get("PAPAGO_CLIENT_SECRET", "")
        self.mymemory_email = cfg.get("MYMEMORY_EMAIL", "")

        self._tok_lock = threading.Lock()
        self._edge_token = ""
        self._edge_exp = 0.0
        self._bing = None          # (key, token, ig, iid)
        self._bing_exp = 0.0

        # 번역 전용 세션(WSCN 수집 세션의 Referer/UA와 분리)
        self._owns_session = session is None
        if session is not None:
            self.s = session
        else:
            self.s = requests.Session()
            adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0)
            self.s.mount("https://", adapter)
            self.s.mount("http://", adapter)
        self.s.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        self.s.headers.pop("Referer", None)

        chosen = (cfg.get("TRANSLATOR", "auto") or "auto").lower()
        order = list(self.AUTO_ORDER)
        if chosen in order:  # 지정 엔진을 맨 앞으로, 나머지는 폴백으로 유지
            order.remove(chosen)
            order.insert(0, chosen)

        available = {
            "deepl": bool(self.deepl_key),
            "azure": bool(self.azure_key),
            "papago": bool(self.papago_id and self.papago_secret),
            "edge": True, "google": True, "bing": True, "mymemory": True,
        }
        # (표시명, 함수, 공식 API 여부) — 공식 API는 문맥을 함께 받아 전체 문장을 한 번에 번역
        fns = {
            "deepl": ("DeepL", self._deepl, True), "azure": ("Azure", self._azure, True),
            "papago": ("Papago", self._papago, True), "edge": ("Edge", self._edge, False),
            "google": ("Google", self._google, False), "bing": ("Bing", self._bing_tr, False),
            "mymemory": ("MyMemory", self._mymemory, False),
        }
        self.engines = [_Engine(*fns[n]) for n in order if available[n]]
        self._deepl_model = True  # 품질 우선 모델 옵션 사용 가능 여부(거부되면 끔)
        self.engine_name = self.engines[0].name
        self.last_engine = ""

    # ── public ───────────────────────────────────────────
    def translate(self, text: str, context: str = "") -> str:
        """번역 성공 시 한국어, 실패 시 "" (호출 측에서 원문 표시 후 재시도).

        context: 같은 속보의 제목/본문 등 참고용 문맥(공식 API에만 전달, 번역되지 않음)
        """
        text = (text or "").strip()[:MAX_CHARS]
        if not text:
            return ""
        if not _HAN_RE.search(text):  # 한자가 없으면(숫자·영문) 번역 불필요
            return text
        ko = self._translate_premium(text, context)
        if ko:
            return ko
        out = []
        for chunk in split_chunks(text):
            ko = self._translate_chunk(chunk.strip())
            if not ko:
                return ""
            trail = chunk[len(chunk.rstrip()):]
            out.append(ko + ("\n" if "\n" in trail else " "))
        return "".join(out).strip()

    def health(self) -> list[dict]:
        now = time.time()
        return [{"engine": e.name, "ok": e.ok, "fail": e.ng,
                 "cooldown": max(0, int(e.until - now))} for e in self.engines]

    def close(self) -> None:
        if self._owns_session and hasattr(self, "s"):
            self.s.close()

    # ── internals ────────────────────────────────────────
    def _translate_premium(self, text: str, context: str) -> str:
        """공식 API: 자르지 않고 문맥과 함께 전체 번역. 결과는 원문 단위로 캐시."""
        key = "p:" + hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        now = time.time()
        for eng in self.engines:
            if not eng.premium or not eng.available(now):
                continue
            try:
                out = (eng.fn(text, context) or "").strip()
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                log.warning("번역 실패(%s): HTTP %s", eng.name, code)
                # 456=DeepL 월 한도 소진, 401/403=키 오류 → 오래 쉬고 무료 엔진으로 폴백
                eng.failure(3600 if code in (401, 403, 456) else None)
                continue
            except Exception as e:
                log.warning("번역 실패(%s): %s", eng.name, type(e).__name__)
                eng.failure()
                continue
            if is_valid_korean(text, out):
                eng.success()
                self.last_engine = eng.name
                self.cache.put(key, out)
                return out
            log.warning("번역 결과 무효(%s) — 다음 엔진 시도", eng.name)
            eng.failure()
        return ""

    def _translate_chunk(self, chunk: str) -> str:
        if not _HAN_RE.search(chunk):
            return chunk
        key = hashlib.blake2b(chunk.encode("utf-8"), digest_size=16).hexdigest()
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        now = time.time()
        for eng in self.engines:
            if eng.premium or not eng.available(now):
                continue
            try:
                out = (eng.fn(chunk) or "").strip()
            except Exception as e:  # 엔진별 예외 형태가 제각각 — 모두 실패로 간주
                log.warning("번역 실패(%s): %s", eng.name, type(e).__name__)
                eng.failure()
                continue
            if is_valid_korean(chunk, out):
                eng.success()
                self.last_engine = eng.name
                self.cache.put(key, out)
                return out
            log.warning("번역 결과 무효(%s) — 다음 엔진 시도", eng.name)
            eng.failure()
        return ""

    # ── engines (공식 API) ───────────────────────────────
    def _deepl(self, text: str, context: str = "") -> str:
        host = "api-free.deepl.com" if self.deepl_key.endswith(":fx") else "api.deepl.com"
        data = {
            "text": text, "source_lang": "ZH", "target_lang": "KO",
            "context": (BASE_CONTEXT + ("\n" + context[:2000] if context else "")),
            "preserve_formatting": "1",  # 줄바꿈·문장부호 유지
        }
        if self._deepl_model:
            data["model_type"] = "prefer_quality_optimized"  # 차세대(고품질) 모델 우선
        r = self.s.post(f"https://{host}/v2/translate",
                        headers={"Authorization": f"DeepL-Auth-Key {self.deepl_key}"},
                        data=data, timeout=self.timeout)
        if r.status_code == 400 and self._deepl_model:  # 옵션 미지원 계정 — 끄고 재시도
            self._deepl_model = False
            data.pop("model_type", None)
            r = self.s.post(f"https://{host}/v2/translate",
                            headers={"Authorization": f"DeepL-Auth-Key {self.deepl_key}"},
                            data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["translations"][0]["text"]

    def _azure(self, text: str, context: str = "") -> str:
        headers = {"Ocp-Apim-Subscription-Key": self.azure_key}
        if self.azure_region:
            headers["Ocp-Apim-Subscription-Region"] = self.azure_region
        r = self.s.post(
            "https://api.cognitive.microsofttranslator.com/translate",
            params={"api-version": "3.0", "from": "zh-Hans", "to": "ko"},
            headers=headers, json=[{"Text": text}], timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()[0]["translations"][0]["text"]

    def _papago(self, text: str, context: str = "") -> str:
        r = self.s.post(
            "https://papago.apigw.ntruss.com/nmt/v1/translation",
            headers={"X-NCP-APIGW-API-KEY-ID": self.papago_id,
                     "X-NCP-APIGW-API-KEY": self.papago_secret},
            data={"source": "zh-CN", "target": "ko", "text": text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()["message"]["result"]["translatedText"]

    # ── engines (무료) ───────────────────────────────────
    def _edge(self, text: str) -> str:
        """Microsoft Edge 내장 번역 엔드포인트(무료 토큰, 클라우드 IP 차단이 적음)."""
        token = self._get_edge_token()
        r = self.s.post(
            "https://api-edge.cognitiveservices.azure.com/translate",
            params={"api-version": "3.0", "from": "zh-Hans", "to": "ko"},
            headers={"Authorization": f"Bearer {token}"},
            json=[{"Text": text}], timeout=self.free_timeout,
        )
        if r.status_code in (401, 403):
            self._edge_token = ""
        r.raise_for_status()
        return r.json()[0]["translations"][0]["text"]

    def _get_edge_token(self) -> str:
        if self._edge_token and time.time() < self._edge_exp:
            return self._edge_token
        with self._tok_lock:
            if self._edge_token and time.time() < self._edge_exp:
                return self._edge_token
            r = self.s.get("https://edge.microsoft.com/translate/auth", timeout=self.free_timeout)
            r.raise_for_status()
            tok = r.text.strip()
            if len(tok) < 20 or tok.count(".") != 2:  # JWT 형식 확인
                raise ValueError("invalid edge token")
            self._edge_token, self._edge_exp = tok, time.time() + 480  # 유효 10분 중 8분 사용
            return tok

    def _google(self, text: str) -> str:
        r = self.s.post(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "zh-CN", "tl": "ko", "dt": "t"},
            data={"q": text}, timeout=self.free_timeout,
        )
        r.raise_for_status()  # 429(CAPTCHA) 등은 예외 → 서킷 브레이커
        data = r.json()
        return "".join(seg[0] for seg in data[0]
                       if isinstance(seg, list) and seg and isinstance(seg[0], str))

    def _get_bing_tokens(self, force: bool = False):
        if not force and self._bing and time.time() < self._bing_exp:
            return self._bing
        with self._tok_lock:
            if not force and self._bing and time.time() < self._bing_exp:
                return self._bing
            r = self.s.get("https://www.bing.com/translator", timeout=self.free_timeout)
            r.raise_for_status()
            ig_m = re.search(r'IG:"([A-Za-z0-9]+)"', r.text)
            iid_m = re.search(r'data-iid="([^"]+)"', r.text)
            abuse_m = re.search(r'params_AbusePreventionHelper\s*=\s*(\[[^;]+\]);', r.text)
            if not (ig_m and abuse_m):
                raise ValueError("bing tokens not found")
            abuse = json.loads(abuse_m.group(1))
            self._bing = (abuse[0], abuse[1], ig_m.group(1),
                          iid_m.group(1) if iid_m else "translator.5028")
            self._bing_exp = time.time() + 1800
            return self._bing

    def _bing_tr(self, text: str) -> str:
        key, token, ig, iid = self._get_bing_tokens()
        r = self.s.post(
            f"https://www.bing.com/ttranslatev3?isVertical=1&IG={ig}&IID={iid}",
            data={"fromLang": "zh-Hans", "to": "ko", "text": text, "token": token, "key": key},
            timeout=self.free_timeout,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):  # 토큰 만료(205) 등 — 다음 호출에서 재발급
            self._bing = None
            raise ValueError(f"bing status {data.get('statusCode')}")
        return data[0]["translations"][0]["text"]

    def _mymemory(self, text: str) -> str:
        params = {"q": text, "langpair": "zh-CN|ko"}
        if self.mymemory_email:  # 이메일 지정 시 일일 한도 5천자 → 5만자
            params["de"] = self.mymemory_email
        r = self.s.get("https://api.mymemory.translated.net/get",
                       params=params, timeout=self.free_timeout)
        r.raise_for_status()
        data = r.json()
        if int(data.get("responseStatus") or 0) != 200:
            raise ValueError("mymemory status")
        return (data.get("responseData") or {}).get("translatedText", "")
