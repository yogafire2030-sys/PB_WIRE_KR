/* Global Wire KR — 실시간 속보 클라이언트
 * - 서버에서 변경분(delta)만 받아 병합 → 트래픽·렌더링 최소화
 * - 데이터는 모두 textContent로만 출력(XSS 차단)
 * - 탭이 숨겨지면 폴링 중지, 요청은 AbortController로 타임아웃
 * - 항목 DOM은 id 기준으로 재사용(불필요한 재생성 방지), 삭제된 항목은 캐시에서도 제거
 */
(() => {
  "use strict";

  if (!Element.prototype.replaceChildren) {
    Element.prototype.replaceChildren = function (...nodes) {
      while (this.firstChild) this.removeChild(this.firstChild);
      this.append(...nodes);
    };
  }

  const boot = JSON.parse(document.getElementById("boot").textContent);
  const $ = (id) => document.getElementById(id);
  const els = {
    feed: $("feed"), empty: $("empty"), meta: $("meta"), tabs: $("tabs"),
    live: $("live"), liveText: $("liveText"), clock: $("clock"), today: $("today"),
    important: $("onlyImportant"), search: $("search"),
    pill: $("newPill"), pillCount: $("newCount"), themeBtn: $("themeBtn"),
    tpl: $("tplItem"), mkt: $("mkt"),
  };

  const POLL_MS = Math.max(3000, Number(boot.pollMs) || 5000);
  const MAX_BACKOFF = 60000;
  const CLAMP_LEN = 220;
  const KST = "Asia/Seoul";

  const fmtTime = new Intl.DateTimeFormat("ko-KR", { timeZone: KST, hour: "2-digit", minute: "2-digit", hour12: false });
  const fmtClock = new Intl.DateTimeFormat("ko-KR", { timeZone: KST, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  const fmtDay = new Intl.DateTimeFormat("ko-KR", { timeZone: KST, month: "long", day: "numeric", weekday: "long" });
  const fmtKey = new Intl.DateTimeFormat("en-CA", { timeZone: KST, year: "numeric", month: "2-digit", day: "2-digit" });

  // ── 상태 ──────────────────────────────────────────────
  const store = {}; // channel -> { cursor, items: Map<id,item>, error, loaded }
  const nodes = new Map(); // id -> element (현재 채널만 보관)
  const prefs = loadPrefs();
  let channel = boot.channel || "global";
  let timer = null, inflight = null, backoff = 0, pending = 0, query = "";

  function loadPrefs() {
    try { return JSON.parse(localStorage.getItem("gw-prefs") || "{}") || {}; } catch { return {}; }
  }
  function savePrefs() {
    try {
      localStorage.setItem("gw-prefs", JSON.stringify({
        important: els.important.checked,
        theme: document.documentElement.dataset.theme,
      }));
    } catch { /* 저장 불가 환경 — 무시 */ }
  }

  // ── 초기화 ────────────────────────────────────────────
  if (prefs.theme === "light" || prefs.theme === "dark") document.documentElement.dataset.theme = prefs.theme;
  else if (matchMedia("(prefers-color-scheme: light)").matches) document.documentElement.dataset.theme = "light";
  els.important.checked = !!prefs.important;
  if (els.tabs) setActiveTab(channel);
  tickClock();
  setInterval(tickClock, 1000);

  // ── 이벤트 ────────────────────────────────────────────
  if (els.tabs) {
    els.tabs.addEventListener("click", (e) => {
      const btn = e.target.closest(".tab");
      if (!btn || btn.dataset.channel === channel) return;
      channel = btn.dataset.channel;
      setActiveTab(channel);
      savePrefs();
      nodes.clear();
      els.feed.replaceChildren();
      hidePill();
      render();
      poll(true);
    });
  }
  els.important.addEventListener("change", () => { savePrefs(); render(); });
  let searchT = 0;
  els.search.addEventListener("input", () => {
    clearTimeout(searchT);
    searchT = setTimeout(() => { query = els.search.value.trim().toLowerCase().slice(0, 60); render(); }, 160);
  });
  els.themeBtn.addEventListener("click", () => {
    const r = document.documentElement;
    r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light";
    savePrefs();
  });
  els.pill.addEventListener("click", () => { window.scrollTo({ top: 0, behavior: "smooth" }); hidePill(); });
  window.addEventListener("scroll", () => { if (window.scrollY < 120) hidePill(); }, { passive: true });

  // 항목 액션은 이벤트 위임 1개로 처리(리스너 누수 방지)
  els.feed.addEventListener("click", (e) => {
    let btn = e.target.closest("button[data-act]");
    if (!btn) {
      // 접힌 본문 클릭 시 펼치기
      const txt = e.target.closest(".item.is-clamped .item__text");
      if (!txt || getSelection().toString()) return;
      btn = txt.closest(".item").querySelector('[data-act="more"]');
    }
    const art = btn.closest(".item");
    const id = Number(art?.dataset.id);
    const it = store[channel]?.items.get(id);
    if (!it) return;
    const act = btn.dataset.act;
    if (act === "more") {
      const clamped = art.classList.toggle("is-clamped");
      btn.textContent = clamped ? "더보기" : "접기";
      art.dataset.open = clamped ? "" : "1";
    } else if (act === "copy") {
      const ko = koOf(it);
      const t = [`[${fmtTime.format(it.ts * 1000)}] ${ko.title || it.title_zh || ""}`.trim(), ko.body || it.body_zh || ""].filter(Boolean).join("\n");
      navigator.clipboard?.writeText(t).then(() => flash(btn, "복사됨"), () => flash(btn, "실패"));
    }
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { stopPolling(); stopMarket(); setLive("paused", "일시정지"); }
    else { poll(true); pollMarket(); pumpTranslate(); }
  });

  // ── 폴링 ──────────────────────────────────────────────
  function stopPolling() {
    clearTimeout(timer); timer = null;
    if (inflight) { inflight.abort(); inflight = null; }
  }

  async function poll(immediate = false) {
    stopPolling();
    if (document.hidden) return;
    if (!immediate) return schedule();
    const ch = channel;
    if (!store[ch]) store[ch] = { cursor: 0, items: new Map(), error: "", loaded: false };
    const st = store[ch];
    const ctrl = new AbortController();
    inflight = ctrl;
    const to = setTimeout(() => ctrl.abort(), 12000);
    try {
      const res = await fetch(`/api/news?channel=${encodeURIComponent(ch)}&since=${st.cursor}`, {
        signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      if (ch !== channel) return; // 응답 도착 전 탭 전환
      const added = merge(st, data);
      backoff = 0;
      if (data.error) setLive("error", "원본 지연"); else setLive("ok", "실시간");
      render(added);
    } catch (err) {
      if (err.name === "AbortError" && ch !== channel) return;
      backoff = Math.min(MAX_BACKOFF, backoff ? backoff * 2 : POLL_MS * 2);
      setLive("error", "재연결 중");
    } finally {
      clearTimeout(to);
      if (inflight === ctrl) inflight = null;
      if (ch === channel) schedule();
    }
  }
  function schedule() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(() => poll(true), backoff || POLL_MS);
  }

  function merge(st, data) {
    const added = new Set();
    const firstLoad = st.cursor === 0;
    if (data.reset) {
      const fresh = new Map();
      for (const it of data.items) fresh.set(it.id, it);
      if (!firstLoad) for (const id of fresh.keys()) if (!st.items.has(id)) added.add(id);
      st.items = fresh;
    } else {
      for (const it of data.items) {
        if (!st.items.has(it.id)) added.add(it.id);
        st.items.set(it.id, it);
        nodes.delete(it.id); // 번역 갱신분 — 다시 그리도록 캐시 무효화
      }
    }
    // 서버 보관 상한과 맞춤(클라이언트 메모리 상한, 3일치 이상 수용)
    if (st.items.size > 1500) {
      const keep = [...st.items.values()].sort(byTimeDesc).slice(0, 1500);
      st.items = new Map(keep.map((x) => [x.id, x]));
    }
    st.cursor = Number(data.cursor) || 0;
    st.error = data.error || "";
    st.loaded = !!data.loaded;
    return added;
  }

  // ── 렌더링 ────────────────────────────────────────────
  function byTimeDesc(a, b) { return b.ts - a.ts || b.id - a.id; }

  function render(added = new Set()) {
    const st = store[channel];
    els.feed.setAttribute("aria-busy", st && st.loaded ? "false" : "true");
    if (!st || (!st.loaded && st.items.size === 0)) { renderMeta(st, 0); return; }

    const onlyHot = els.important.checked;
    const list = [...st.items.values()].filter((it) => {
      if (onlyHot && !it.important) return false;
      if (!query) return true;
      const ko = koOf(it);
      return (ko.title + "\n" + ko.body + "\n" + it.title_zh + "\n" + it.body_zh).toLowerCase().includes(query);
    }).sort(byTimeDesc);

    const frag = document.createDocumentFragment();
    const alive = new Set();
    let lastDay = "";
    for (const it of list) {
      const dk = fmtKey.format(it.ts * 1000);
      if (dk !== lastDay) {
        lastDay = dk;
        const h = document.createElement("div");
        h.className = "day";
        h.textContent = fmtDay.format(it.ts * 1000);
        frag.appendChild(h);
      }
      let node = nodes.get(it.id);
      // 데이터 객체가 바뀌었으면(번역 완료·서버 재시작 후 reset 등) 다시 그림
      if (!node || node.dataset.q !== query || node._it !== it || node._ko !== clientTr.get(it.id)) {
        node = buildItem(it); nodes.set(it.id, node);
      }
      if (added.has(it.id)) {
        node.classList.remove("is-fresh"); void node.offsetWidth; node.classList.add("is-fresh");
      }
      alive.add(it.id);
      frag.appendChild(node);
    }
    for (const id of nodes.keys()) if (!alive.has(id)) nodes.delete(id); // 메모리 누수 방지
    queueClientTranslate(list);
    els.feed.replaceChildren(frag);
    els.empty.hidden = list.length > 0;
    renderMeta(st, list.length);

    const visibleAdded = list.filter((x) => added.has(x.id)).length;
    if (visibleAdded && window.scrollY > 200) {
      pending += visibleAdded;
      els.pillCount.textContent = String(pending);
      els.pill.hidden = false;
    }
  }

  function buildItem(it) {
    const node = els.tpl.content.firstElementChild.cloneNode(true);
    node.dataset.id = String(it.id);
    node.dataset.q = query;
    node._it = it;
    node._ko = clientTr.get(it.id);
    if (it.important) node.classList.add("is-hot");
    const time = node.querySelector(".item__time");
    time.textContent = fmtTime.format(it.ts * 1000);
    time.dateTime = new Date(it.ts * 1000).toISOString();

    const title = node.querySelector(".item__title");
    const text = node.querySelector(".item__text");
    const ko = koOf(it);
    paint(title, ko.title || it.title_zh || "속보");
    paint(text, ko.body || it.body_zh || "");
    if (!it.translated && !node._ko) {
      const tag = document.createElement("span");
      tag.className = "tag"; tag.textContent = "번역 중";
      title.appendChild(tag);
    }

    const more = node.querySelector('[data-act="more"]');
    if ((ko.body || "").length > CLAMP_LEN) node.classList.add("is-clamped");
    else more.hidden = true;
    const a = node.querySelector("a.link-btn");
    a.href = /^https:\/\/wallstreetcn\.com\//.test(it.url) ? it.url : "https://wallstreetcn.com/live/global";
    return node;
  }

  // 검색어 하이라이트 — 텍스트 노드 + <mark>로만 구성(HTML 주입 없음)
  function paint(el, str) {
    el.textContent = "";
    if (!str) return;
    if (!query) { el.textContent = str; return; }
    const low = str.toLowerCase();
    let i = 0, j;
    while ((j = low.indexOf(query, i)) !== -1) {
      if (j > i) el.appendChild(document.createTextNode(str.slice(i, j)));
      const m = document.createElement("mark");
      m.textContent = str.slice(j, j + query.length);
      el.appendChild(m);
      i = j + query.length;
    }
    if (i < str.length) el.appendChild(document.createTextNode(str.slice(i)));
  }

  function renderMeta(st, shown) {
    els.meta.replaceChildren();
    if (!st) return;
    const total = st.items.size;
    const hot = [...st.items.values()].filter((x) => x.important).length;
    const b1 = document.createElement("b"); b1.textContent = `${shown.toLocaleString()}건 표시`;
    els.meta.append(b1, ` · 전체 ${total.toLocaleString()}건 · 중요 ${hot}건`);
    if (st.error) {
      const e = document.createElement("span"); e.className = "err"; e.textContent = ` · ${st.error}`;
      els.meta.append(e);
    }
  }

  // ── 보조 ──────────────────────────────────────────────
  function setActiveTab(ch) {
    if (!els.tabs) return;
    for (const t of els.tabs.querySelectorAll(".tab")) {
      const on = t.dataset.channel === ch;
      t.classList.toggle("is-active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    }
  }
  function setLive(state, text) { els.live.dataset.state = state; els.liveText.textContent = text; }
  function hidePill() { pending = 0; els.pill.hidden = true; }
  function flash(btn, msg) {
    const old = btn.textContent; btn.textContent = msg; btn.disabled = true;
    setTimeout(() => { btn.textContent = old; btn.disabled = false; }, 1200);
  }
  function tickClock() {
    const now = new Date();
    els.clock.textContent = fmtClock.format(now);
    els.today.textContent = fmtDay.format(now);
  }

  // ── 브라우저 번역 폴백 ─────────────────────────────────
  // 서버(클라우드 IP)의 무료 번역이 차단된 경우, 보고 있는 기기에서 직접 번역해 표시.
  // 결과는 이 화면에서만 쓰며 서버로 보내지 않음(위변조 경로 차단). 서버 번역이 오면 그쪽이 우선.
  const clientTr = new Map(); // id -> { title, body }
  const trQueue = [], trQueued = new Set();
  const TR_MAX_QUEUE = 40, TR_CONCURRENCY = 2, TR_CHUNK = 1000;
  const HAN = /[\u3400-\u9fff\uf900-\ufaff]/, HANGUL = /[\uac00-\ud7a3]/;
  let trBusy = 0, trBlockedUntil = 0, trRetryT = 0;

  function koOf(it) {
    if (it.translated) return { title: it.title_ko || "", body: it.body_ko || "" };
    const c = clientTr.get(it.id);
    return { title: it.title_ko || c?.title || "", body: it.body_ko || c?.body || "" };
  }

  function queueClientTranslate(list) {
    for (const it of list) {
      if (trQueue.length >= TR_MAX_QUEUE) break;
      if (it.translated || clientTr.has(it.id) || trQueued.has(it.id)) continue;
      trQueue.push(it.id); trQueued.add(it.id);
    }
    // 메모리 상한 — 서버 번역이 끝났거나 사라진 항목의 결과는 정리
    if (clientTr.size > 300) {
      const items = store[channel]?.items;
      for (const id of clientTr.keys()) { const x = items?.get(id); if (!x || x.translated) clientTr.delete(id); }
    }
    pumpTranslate();
  }

  function pumpTranslate() {
    if (document.hidden) return;
    const wait = trBlockedUntil - Date.now();
    if (wait > 0) { clearTimeout(trRetryT); trRetryT = setTimeout(pumpTranslate, wait); return; }
    while (trBusy < TR_CONCURRENCY && trQueue.length) {
      const id = trQueue.shift();
      const it = store[channel]?.items.get(id);
      if (!it || it.translated) { trQueued.delete(id); continue; }
      trBusy++;
      translateItemInBrowser(it).then((res) => {
        trQueued.delete(id);
        if (res) { clientTr.set(id, res); scheduleRender(); }
      }, () => {
        trQueued.delete(id);
        trBlockedUntil = Date.now() + 60000; // 차단·오류 시 1분 휴식 후 재시도
      }).finally(() => { trBusy--; pumpTranslate(); });
    }
  }

  async function translateItemInBrowser(it) {
    const title = it.title_ko || (it.title_zh ? await gtx(it.title_zh) : "");
    const body = it.body_ko || (it.body_zh ? await gtx(it.body_zh) : "");
    return title || body ? { title, body } : null;
  }

  async function gtx(text) {
    if (!HAN.test(text)) return text;
    const parts = [];
    for (let i = 0; i < text.length; i += TR_CHUNK) {
      // 문장 경계에서 자르기
      let end = Math.min(text.length, i + TR_CHUNK);
      if (end < text.length) {
        const cut = Math.max(text.lastIndexOf("。", end), text.lastIndexOf("\n", end));
        if (cut > i + 200) end = cut + 1;
      }
      parts.push(text.slice(i, end));
      i = end - TR_CHUNK;
    }
    let out = "";
    for (const part of parts) {
      const ctrl = new AbortController();
      const to = setTimeout(() => ctrl.abort(), 8000);
      try {
        const res = await fetch("https://translate.googleapis.com/translate_a/single?client=gtx&sl=zh-CN&tl=ko&dt=t", {
          method: "POST", signal: ctrl.signal, credentials: "omit", referrerPolicy: "no-referrer",
          headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          body: "q=" + encodeURIComponent(part),
        });
        if (!res.ok) throw new Error(String(res.status));
        const data = await res.json();
        const seg = Array.isArray(data?.[0]) ? data[0].map((s) => (Array.isArray(s) && typeof s[0] === "string" ? s[0] : "")).join("") : "";
        if (!HANGUL.test(seg)) throw new Error("invalid");
        out += seg;
      } finally { clearTimeout(to); }
    }
    return out.trim();
  }

  let renderRaf = 0;
  function scheduleRender() {
    if (renderRaf) return;
    renderRaf = requestAnimationFrame(() => { renderRaf = 0; render(); });
  }

  // ── 주요 지수 ─────────────────────────────────────────
  const fmtNum = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const tiles = new Map(); // key -> { el, price, diff, pct, arrow, time, last }
  if (els.mkt) {
    for (const el of els.mkt.querySelectorAll(".q")) {
      tiles.set(el.dataset.key, {
        el, last: null,
        price: el.querySelector(".q__price"), diff: el.querySelector(".q__diff"),
        pct: el.querySelector(".q__pct"), arrow: el.querySelector(".q__arrow"),
        time: el.querySelector(".q__time"),
        line: el.querySelector(".q__line"), area: el.querySelector(".q__area"),
        pct6: el.querySelector(".q__6mpct"), hist: null,
      });
    }
  }
  let mktTimer = null, mktCtrl = null, mktBackoff = 0;

  function stopMarket() {
    clearTimeout(mktTimer); mktTimer = null;
    if (mktCtrl) { mktCtrl.abort(); mktCtrl = null; }
  }

  async function pollMarket() {
    stopMarket();
    if (!tiles.size || document.hidden) return;
    const ctrl = new AbortController();
    mktCtrl = ctrl;
    const to = setTimeout(() => ctrl.abort(), 10000);
    let next = 15000;
    try {
      const res = await fetch("/api/market", {
        signal: ctrl.signal, cache: "no-store", credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const items = Array.isArray(data.items) ? data.items : [];
      for (const q of items) updateTile(q);
      mktBackoff = 0;
      // 첫 수집 전(빈 응답)에는 짧게 재시도
      next = items.length ? Math.max(5000, (Number(data.interval) || 15) * 1000) : 3000;
    } catch (err) {
      if (err.name === "AbortError" && mktCtrl !== ctrl) return; // 탭 숨김 등으로 취소
      mktBackoff = Math.min(MAX_BACKOFF, mktBackoff ? mktBackoff * 2 : 10000);
      next = mktBackoff;
    } finally {
      clearTimeout(to);
      if (mktCtrl === ctrl) {
        mktCtrl = null;
        if (!document.hidden) mktTimer = setTimeout(pollMarket, next);
      }
    }
  }

  function updateTile(q) {
    const t = tiles.get(q.key);
    if (!t || !Number.isFinite(q.price)) return;
    const chg = Number(q.change) || 0, pct = Number(q.pct) || 0;
    const sign = chg > 0 ? "+" : chg < 0 ? "-" : "";
    const el = t.el;
    el.classList.remove("is-loading", "is-up", "is-down");
    if (chg > 0) el.classList.add("is-up"); else if (chg < 0) el.classList.add("is-down");
    el.classList.toggle("is-stale", !!q.stale);
    t.price.textContent = fmtNum.format(q.price);
    t.arrow.textContent = chg > 0 ? "▲" : chg < 0 ? "▼" : "■";
    t.diff.textContent = sign + fmtNum.format(Math.abs(chg));
    t.pct.textContent = (pct > 0 ? "+" : pct < 0 ? "-" : "") + fmtNum.format(Math.abs(pct)) + "%";
    t.time.textContent = `${q.state === "CLOSE" ? "CLOSE" : "LAST"} | ${q.time || ""}`;
    el.title = q.stale ? "시세 지연 — 마지막 수신값" : "";
    if (t.last !== null && t.last !== q.price) {
      el.classList.remove("is-tick"); void el.offsetWidth; el.classList.add("is-tick");
    }
    t.last = q.price;
  }

  // ── 6개월 추이(스파크라인) ────────────────────────────
  let histTimer = null;
  async function loadHistory() {
    clearTimeout(histTimer);
    let next = 15 * 60000;
    try {
      const res = await fetch("/api/market/history", { cache: "no-store", credentials: "same-origin" });
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const items = data && typeof data.items === "object" ? data.items : {};
      let n = 0;
      for (const [key, t] of tiles) {
        const h = items[key];
        const c = Array.isArray(h?.c) ? h.c.map(Number).filter(Number.isFinite) : [];
        if (c.length >= 2) { t.hist = c; n++; drawSpark(t); }
      }
      if (n < tiles.size) next = 20000; // 서버 첫 수집 전이거나 일부 실패 — 잠시 후 재시도
    } catch { next = 60000; }
    histTimer = setTimeout(loadHistory, next);
  }

  function drawSpark(t) {
    const h = t.hist;
    t.el.classList.toggle("is-nohist", !h);
    if (!h) return;
    const c = h; // 15분 주기 데이터 그대로(실시간 가격과 섞지 않음)
    let lo = Infinity, hi = -Infinity;
    for (const v of c) { if (v < lo) lo = v; if (v > hi) hi = v; }
    const span = hi - lo || 1, n = c.length - 1;
    let d = "";
    for (let i = 0; i <= n; i++) {
      const x = (i / n) * 100, y = 28 - ((c[i] - lo) / span) * 26;
      d += (i ? "L" : "M") + x.toFixed(2) + " " + y.toFixed(2);
    }
    t.line.setAttribute("d", d);
    t.area.setAttribute("d", d + "L100 30L0 30Z");
    const p6 = ((c[n] - c[0]) / c[0]) * 100;
    t.pct6.textContent = (p6 > 0 ? "+" : p6 < 0 ? "-" : "") + fmtNum.format(Math.abs(p6)) + "%";
  }

  poll(true);
  pollMarket();
  if (tiles.size) loadHistory();
})();
