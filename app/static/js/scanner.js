/* SEPA 스크리너(/scanner) – 클라이언트 로직
 * - 모든 외부 데이터는 textContent로만 렌더링 (XSS 방지)
 * - 서버 재시작으로 보안 토큰이 바뀌면 새 토큰을 받아 1회 자동 재시도
 * - 이벤트 위임 + DocumentFragment 일괄 렌더링 (성능)
 * - AbortController로 중복 요청 취소 (메모리/네트워크 낭비 방지)
 */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const API = "/scanner/api";

  // ── 테마(메인 화면과 같은 설정 공유) ──
  const PREFS_KEY = "gw-prefs";
  function loadPrefs() { try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") || {}; } catch { return {}; } }
  {
    const p = loadPrefs();
    if (p.theme === "light" || p.theme === "dark") document.documentElement.dataset.theme = p.theme;
    else if (matchMedia("(prefers-color-scheme: light)").matches) document.documentElement.dataset.theme = "light";
  }
  $("themeBtn")?.addEventListener("click", () => {
    const r = document.documentElement;
    r.dataset.theme = r.dataset.theme === "light" ? "dark" : "light";
    try { localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPrefs(), theme: r.dataset.theme })); } catch { /* 무시 */ }
  });
  const CHECK_KEYS = Array.from(document.querySelectorAll("#dChecks li")).map((li) => li.dataset.check);
  const NUM_FMT = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const INT_FMT = new Intl.NumberFormat("en-US");

  const state = {
    rows: [],
    view: [],
    buzz: new Map(),
    sortKey: null,
    sortDir: -1,
    filter: "",
    screenCtrl: null,
    buzzCtrl: null,
    histCtrl: null,
  };

  const els = {
    form: $("screenForm"), runBtn: $("runBtn"), resetBtn: $("resetBtn"),
    body: $("resultBody"), table: $("resultTable"), notice: $("notice"),
    status: $("statusText"), statusDot: $("statusDot"),
    tableFilter: $("tableFilter"), dataInfo: $("dataInfo"),
    buzzBtn: $("buzzBtn"), buzzCount: $("buzzCount"), csvBtn: $("csvBtn"),
    kpiTotal: $("kpiTotal"), kpiPass: $("kpiPass"), kpiBuzz: $("kpiBuzz"), kpiSurge: $("kpiSurge"),
    drawer: $("drawer"), backdrop: $("drawerBackdrop"),
  };

  /* ---------- 공통 유틸 ---------- */
  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  const vol = (v) => (!isNum(v) ? "—" : v >= 1e6 ? `${NUM_FMT.format(v / 1e6)}M` : `${NUM_FMT.format(v / 1e3)}K`);
  const usd = (v) => (isNum(v) ? `$${NUM_FMT.format(v)}` : "—");
  const pct = (v) => (isNum(v) ? `${v > 0 ? "+" : ""}${NUM_FMT.format(v)}%` : "—");
  const signCls = (v) => (!isNum(v) ? "dim" : v > 0 ? "pos" : v < 0 ? "neg" : "");

  function setStatus(text, busy) {
    els.status.textContent = text;
    els.statusDot.className = `s-dot ${busy ? "busy" : "ready"}`;
  }
  function notice(msg, type = "info") {
    if (!msg) { els.notice.hidden = true; return; }
    els.notice.className = `notice ${type}`;
    els.notice.textContent = msg;
    els.notice.hidden = false;
  }
  function loading(btn, on) {
    btn.classList.toggle("loading", on);
    btn.disabled = on;
  }

  async function refreshToken() {
    const res = await fetch(`${API}/csrf`, { credentials: "same-origin", cache: "no-store" });
    const data = await res.json();
    if (typeof data?.token === "string" && data.token) CSRF = data.token;
  }

  async function api(url, body, ctrl, retried = false) {
    const opts = { method: body ? "POST" : "GET", headers: { "X-CSRF-Token": CSRF }, signal: ctrl?.signal, credentials: "same-origin" };
    if (body) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch { /* 비 JSON 응답 */ }
    if (res.status === 403 && data?.code === "csrf" && !retried) {
      await refreshToken();          // 서버가 재시작돼 토큰이 바뀐 경우
      return api(url, body, ctrl, true);
    }
    if (!res.ok) throw new Error((data && data.error) || `요청 실패 (HTTP ${res.status})`);
    return data;
  }

  /* ---------- 스크리닝 ---------- */
  function collectForm() {
    const params = {};
    els.form.querySelectorAll("[data-param]").forEach((i) => { params[i.name] = i.type === "number" ? Number(i.value) : i.value; });
    params.strict = $("strict").checked;
    return { params };
  }

  const sleep = (ms, signal) => new Promise((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("aborted", "AbortError")); }, { once: true });
  });

  // 서버가 전 종목을 처음 계산하는 동안(약 1~2분) 진행률을 보여 주며 대기
  async function waitReady(ctrl) {
    for (let i = 0; i < 120; i++) {
      const s = await api(`${API}/status`, null, ctrl);
      if (s.ready) return;
      const pctDone = s.total ? Math.round((s.done / s.total) * 100) : 0;
      setStatus(`데이터 준비 중… ${s.done}/${s.total || "?"} (${pctDone}%)`, true);
      notice("서버가 S&P 500 + 나스닥 100 전 종목의 1년 주가를 계산하고 있습니다. 처음 한 번만 1~2분 걸리며, 끝나면 자동으로 결과가 표시됩니다.", "info");
      await sleep(3000, ctrl.signal);
    }
    throw new Error("데이터 준비가 지연되고 있습니다. 잠시 후 다시 시도하세요.");
  }

  function showDataInfo(st) {
    if (!st?.updated) { els.dataInfo.textContent = ""; return; }
    const t = new Date(st.updated * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit", hour12: false });
    els.dataInfo.textContent = `Yahoo 일봉 기준 · ${t} 계산 · ${INT_FMT.format(st.count)}종목${st.running ? " · 갱신 중" : ""}`;
  }

  async function runScreen(ev) {
    ev?.preventDefault();
    state.screenCtrl?.abort();
    const ctrl = (state.screenCtrl = new AbortController());
    loading(els.runBtn, true);
    notice(null);
    setStatus("스크리닝 중…", true);
    try {
      let data = await api(`${API}/screen`, collectForm(), ctrl);
      if (data.preparing) {
        await waitReady(ctrl);
        notice(null);
        data = await api(`${API}/screen`, collectForm(), ctrl);
      }
      state.rows = data.rows;
      state.buzz.clear();
      showDataInfo(data.status);
      els.kpiTotal.textContent = INT_FMT.format(data.total);
      els.kpiPass.textContent = INT_FMT.format(data.full);
      els.kpiBuzz.textContent = "—";
      els.kpiSurge.textContent = "—";
      els.buzzBtn.disabled = data.rows.length === 0;
      els.csvBtn.disabled = data.rows.length === 0;
      if (!data.rows.length) {
        notice("조건에 맞는 종목이 없습니다. 필터를 완화해 보세요.", "warn");
      }
      applyView();
      setStatus(`완료 · ${data.rows.length}종목`, false);
      // 좁은 화면(조건 패널이 위에 쌓이는 배치)에서는 결과로 자동 이동
      if (matchMedia("(max-width: 860px)").matches) document.querySelector(".scan .content")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      if (err.name === "AbortError") return;
      notice(err.message, "error");
      setStatus("오류", false);
    } finally {
      if (state.screenCtrl === ctrl) { state.screenCtrl = null; loading(els.runBtn, false); }
    }
  }

  /* ---------- 테이블 ---------- */
  function sortValue(r, key) {
    const b = state.buzz.get(r.ticker);
    if (key === "buzz24") return b?.ok ? (b.saturated ? 1e6 + b.msgs_24h : b.msgs_24h) : null;
    if (key === "buzzChg") return b?.ok ? (b.saturated ? 1e6 : b.change_pct) : null;
    if (key === "watchers") return b?.ok ? b.watchers : null;
    return r[key];
  }

  function applyView() {
    const q = state.filter;
    let v = q
      ? state.rows.filter((r) => `${r.ticker} ${r.company} ${r.exchange}`.toLowerCase().includes(q))
      : state.rows.slice();
    if (state.sortKey) {
      const k = state.sortKey, d = state.sortDir;
      v.sort((a, b) => {
        const x = sortValue(a, k), y = sortValue(b, k);
        if (x === y) return 0;
        if (x === null || x === undefined) return 1;
        if (y === null || y === undefined) return -1;
        return (typeof x === "string" ? x.localeCompare(y) : x - y) * d;
      });
    }
    state.view = v;
    render();
  }

  function numCell(v, cls = "", text) {
    const td = el("td", `num ${cls}`.trim(), text ?? (isNum(v) ? NUM_FMT.format(v) : "—"));
    return td;
  }

  function buildRow(r) {
    const tr = el("tr");
    tr.dataset.ticker = r.ticker;
    if (r.passed === r.total) tr.classList.add("full");

    const tdT = el("td");
    tdT.append(el("span", "tk", r.ticker));
    tr.append(tdT);

    const tdC = el("td", "col-company", r.company || "");
    tdC.append(el("span", "sub", r.exchange || ""));
    tr.append(tdC);

    const tdChk = el("td", "num");
    const dots = el("span", "checks");
    for (const k of CHECK_KEYS) {
      const v = r.checks?.[k];
      dots.append(el("span", `chk ${v === true ? "on" : v === false ? "off" : "na"}`));
    }
    tdChk.append(dots, document.createTextNode(`${r.passed}/${r.total}`));
    tr.append(tdChk);

    const tdRs = el("td", "num");
    if (isNum(r.rs_rank)) tdRs.append(el("span", `rs ${r.rs_rank >= 80 ? "hi" : r.rs_rank >= 50 ? "mid" : "lo"}`, r.rs_rank));
    else tdRs.textContent = "—";
    tr.append(tdRs);

    tr.append(numCell(r.price, "", isNum(r.price) ? `$${NUM_FMT.format(r.price)}` : "—"));
    tr.append(numCell(r.change, signCls(r.change), pct(r.change)));
    tr.append(numCell(r.perf_y, signCls(r.perf_y), pct(r.perf_y)));
    tr.append(numCell(r.from_high, "", pct(r.from_high)));
    tr.append(numCell(r.from_low, "", pct(r.from_low)));
    tr.append(numCell(r.sma50_pct, signCls(r.sma50_pct), pct(r.sma50_pct)));
    tr.append(numCell(r.sma200_pct, signCls(r.sma200_pct), pct(r.sma200_pct)));
    tr.append(numCell(r.avg_volume, "", vol(r.avg_volume)));

    const b = state.buzz.get(r.ticker);
    const td24 = el("td", "num"), tdChg = el("td", "num"), tdW = el("td", "num");
    if (!b) {
      td24.textContent = tdChg.textContent = tdW.textContent = "·";
      td24.className = tdChg.className = tdW.className = "num dim";
    } else if (!b.ok) {
      td24.textContent = b.error || "실패";
      td24.className = "num dim";
      tdChg.textContent = tdW.textContent = "—";
    } else {
      td24.textContent = b.saturated ? `${b.msgs_24h}+` : INT_FMT.format(b.msgs_24h);
      if (b.trending) td24.append(el("span", "s-tag trend", "TREND"));
      if (b.saturated) {
        tdChg.append(el("span", "s-tag hot", "폭증"));
      } else {
        tdChg.textContent = pct(b.change_pct);
        tdChg.className = `num ${signCls(b.change_pct)}`;
        if (isNum(b.change_pct) && b.change_pct >= 50 && b.msgs_24h >= 5) tdChg.append(el("span", "s-tag hot", "급증"));
      }
      tdW.textContent = isNum(b.watchers) ? INT_FMT.format(b.watchers) : "—";
      if (isNum(b.watchers_delta) && b.watchers_delta !== 0) {
        tdW.append(el("span", `sub ${b.watchers_delta > 0 ? "pos" : "neg"}`, `${b.watchers_delta > 0 ? "+" : ""}${INT_FMT.format(b.watchers_delta)} (7일)`));
      }
    }
    tr.append(td24, tdChg, tdW);
    return tr;
  }

  function render() {
    const frag = document.createDocumentFragment();
    if (!state.view.length) {
      const tr = el("tr", "s-empty");
      const td = el("td", "", state.rows.length ? "검색 결과가 없습니다." : "조건에 맞는 종목이 없습니다.");
      td.colSpan = 15;
      tr.append(td);
      frag.append(tr);
    } else {
      for (const r of state.view) frag.append(buildRow(r));
    }
    els.body.replaceChildren(frag);
  }

  function onSort(ev) {
    const th = ev.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    if (state.sortKey === key) state.sortDir *= -1;
    else { state.sortKey = key; state.sortDir = ["ticker", "company"].includes(key) ? 1 : -1; }
    els.table.querySelectorAll("th").forEach((h) => h.classList.remove("sorted", "asc"));
    th.classList.add("sorted");
    if (state.sortDir === 1) th.classList.add("asc");
    applyView();
  }

  let filterTimer = 0;
  function onFilterInput() {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(() => {
      state.filter = els.tableFilter.value.trim().toLowerCase();
      applyView();
    }, 150);
  }

  /* ---------- Stocktwits 버즈 ---------- */
  async function runBuzz() {
    const n = Math.min(Math.max(parseInt(els.buzzCount.value, 10) || 20, 1), 30);
    const symbols = (state.view.length ? state.view : state.rows).slice(0, n).map((r) => r.ticker);
    if (!symbols.length) return;
    state.buzzCtrl?.abort();
    const ctrl = (state.buzzCtrl = new AbortController());
    loading(els.buzzBtn, true);
    setStatus(`Stocktwits 언급량 분석 중… (${symbols.length}종목)`, true);
    try {
      const data = await api(`${API}/buzz`, { symbols }, ctrl);
      let failed = 0;
      for (const b of data.results) {
        state.buzz.set(b.symbol, b);
        if (!b.ok) failed++;
      }
      updateBuzzKpis();
      applyView();
      if (failed === data.results.length) {
        notice("Stocktwits 데이터를 가져오지 못했습니다. 서버 접속 제한(429/403)일 수 있으니 잠시 후 다시 시도하세요.", "error");
      } else if (failed) {
        notice(`${failed}개 종목은 Stocktwits 조회에 실패했습니다.`, "warn");
      } else {
        notice("버즈 분석 완료. 같은 분석을 시간을 두고 반복하면 스냅샷이 쌓여 언급량 추이를 확인할 수 있습니다.", "info");
      }
      setStatus("버즈 분석 완료", false);
    } catch (err) {
      if (err.name === "AbortError") return;
      notice(err.message, "error");
      setStatus("오류", false);
    } finally {
      if (state.buzzCtrl === ctrl) { state.buzzCtrl = null; loading(els.buzzBtn, false); }
    }
  }

  function updateBuzzKpis() {
    let ok = 0, surge = 0;
    for (const b of state.buzz.values()) {
      if (!b.ok) continue;
      ok++;
      if (b.saturated || (isNum(b.change_pct) && b.change_pct >= 50 && b.msgs_24h >= 5)) surge++;
    }
    els.kpiBuzz.textContent = INT_FMT.format(ok);
    els.kpiSurge.textContent = INT_FMT.format(surge);
  }

  /* ---------- 상세 드로어 ---------- */
  function stat(label, value, cls = "") {
    const d = el("div", "stat");
    d.append(el("span", "", label), el("b", cls, value));
    return d;
  }

  function openDrawer(ticker) {
    const r = state.rows.find((x) => x.ticker === ticker);
    if (!r) return;
    $("dTicker").textContent = r.ticker;
    $("dCompany").textContent = [r.company, r.exchange].filter(Boolean).join(" · ");
    document.querySelectorAll("#dChecks li").forEach((li) => {
      const v = r.checks?.[li.dataset.check];
      li.firstElementChild.className = `chk ${v === true ? "on" : v === false ? "off" : "na"}`;
    });
    $("dStats").replaceChildren(
      stat("50일선", usd(r.sma50)),
      stat("150일선", usd(r.sma150)),
      stat("200일선", usd(r.sma200)),
      stat("50일선 대비", pct(r.sma50_pct), signCls(r.sma50_pct)),
      stat("150일선 대비", pct(r.sma150_pct), signCls(r.sma150_pct)),
      stat("200일선 대비", pct(r.sma200_pct), signCls(r.sma200_pct)),
      stat("52주 고가", usd(r.high52)),
      stat("52주 저가", usd(r.low52)),
      stat("RSI(14)", isNum(r.rsi) ? NUM_FMT.format(r.rsi) : "—"),
      stat("분기 수익률", pct(r.perf_q), signCls(r.perf_q)),
      stat("반기 수익률", pct(r.perf_h), signCls(r.perf_h)),
      stat("상대 거래량", isNum(r.rel_volume) ? `${NUM_FMT.format(r.rel_volume)}x` : "—"),
    );
    $("dYahoo").href = `https://finance.yahoo.com/quote/${encodeURIComponent(r.ticker)}`;
    $("dFinviz").href = `https://finviz.com/quote.ashx?t=${encodeURIComponent(r.ticker)}`;
    $("dStocktwits").href = `https://stocktwits.com/symbol/${encodeURIComponent(r.ticker)}`;
    els.drawer.classList.add("open");
    els.drawer.setAttribute("aria-hidden", "false");
    els.backdrop.hidden = false;
    loadHistory(r.ticker);
  }

  function closeDrawer() {
    els.drawer.classList.remove("open");
    els.drawer.setAttribute("aria-hidden", "true");
    els.backdrop.hidden = true;
    state.histCtrl?.abort();
  }

  async function loadHistory(ticker) {
    state.histCtrl?.abort();
    const ctrl = (state.histCtrl = new AbortController());
    const box = $("dChart");
    box.textContent = "불러오는 중…";
    try {
      const data = await api(`${API}/buzz/history/${encodeURIComponent(ticker)}`, null, ctrl);
      drawChart(box, data.history);
    } catch (err) {
      if (err.name !== "AbortError") box.textContent = err.message;
    }
  }

  const SVG_NS = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  }

  function drawChart(box, hist) {
    if (!hist || hist.length < 2) {
      box.textContent = hist && hist.length === 1
        ? `스냅샷 1개 (24h 언급 ${hist[0].msgs_24h}건). 시간을 두고 버즈 분석을 반복하면 추이 그래프가 표시됩니다.`
        : "저장된 스냅샷이 없습니다. 버즈 분석을 실행하면 기록이 시작됩니다.";
      return;
    }
    const W = 380, H = 150, P = { l: 32, r: 8, t: 10, b: 20 };
    const vals = hist.map((h) => h.msgs_24h);
    const max = Math.max(...vals, 1);
    const t0 = hist[0].ts, t1 = hist[hist.length - 1].ts || t0 + 1;
    const x = (t) => P.l + ((t - t0) / Math.max(t1 - t0, 1)) * (W - P.l - P.r);
    const y = (v) => H - P.b - (v / max) * (H - P.t - P.b);
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "24시간 언급 건수 추이" });
    s.append(svg("line", { class: "axis", x1: P.l, x2: W - P.r, y1: H - P.b, y2: H - P.b }));
    let d = "";
    hist.forEach((h, i) => { d += `${i ? "L" : "M"}${x(h.ts).toFixed(1)},${y(h.msgs_24h).toFixed(1)}`; });
    s.append(svg("path", { class: "area", d: `${d}L${x(t1).toFixed(1)},${H - P.b}L${P.l},${H - P.b}Z` }));
    s.append(svg("path", { class: "line", d }));
    if (hist.length <= 40) hist.forEach((h) => s.append(svg("circle", { class: "pt", cx: x(h.ts), cy: y(h.msgs_24h), r: 2.5 })));
    const lab = (txt, ax, ay, anchor = "start") => { const t = svg("text", { x: ax, y: ay, "text-anchor": anchor }); t.textContent = txt; s.append(t); };
    lab(String(max), P.l - 4, P.t + 8, "end");
    lab("0", P.l - 4, H - P.b, "end");
    const fmt = (ts) => new Date(ts * 1000).toLocaleDateString("ko-KR", { month: "numeric", day: "numeric" });
    lab(fmt(t0), P.l, H - 4);
    lab(fmt(t1), W - P.r, H - 4, "end");
    box.replaceChildren(s);
  }

  /* ---------- CSV ---------- */
  function csvCell(v) {
    let s = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s) && !/^-?\d/.test(s)) s = `'${s}`; // CSV 수식 인젝션 방지
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  }
  function exportCsv() {
    const head = ["Ticker", "Company", "Exchange", "Passed", "RS", "Price", "Change%", "PerfYear%", "From52WHigh%", "From52WLow%",
      "vsSMA50%", "vsSMA150%", "vsSMA200%", "AvgVolume50", "Mentions24h", "MentionsPrev24h", "MentionChange%", "Watchers"];
    const lines = [head.join(",")];
    for (const r of state.view) {
      const b = state.buzz.get(r.ticker);
      lines.push([r.ticker, r.company, r.exchange, `${r.passed}/${r.total}`, r.rs_rank, r.price, r.change, r.perf_y,
        r.from_high, r.from_low, r.sma50_pct, r.sma150_pct, r.sma200_pct, isNum(r.avg_volume) ? Math.round(r.avg_volume) : "",
        b?.ok ? b.msgs_24h : "", b?.ok ? b.msgs_prev24h : "", b?.ok ? b.change_pct : "", b?.ok ? b.watchers : ""].map(csvCell).join(","));
    }
    const blob = new Blob(["﻿" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = el("a");
    a.href = url;
    a.download = `sepa_scan_${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000); // Blob URL 해제 (메모리 누수 방지)
  }

  /* ---------- 초기화 ---------- */
  function resetDefaults() {
    els.form.reset();
  }

  els.form.addEventListener("submit", runScreen);
  els.resetBtn.addEventListener("click", resetDefaults);
  els.table.tHead.addEventListener("click", onSort);
  els.body.addEventListener("click", (ev) => {
    const tr = ev.target.closest("tr[data-ticker]");
    if (tr) openDrawer(tr.dataset.ticker);
  });
  els.tableFilter.addEventListener("input", onFilterInput);
  els.buzzBtn.addEventListener("click", runBuzz);
  els.csvBtn.addEventListener("click", exportCsv);
  $("drawerClose").addEventListener("click", closeDrawer);
  els.backdrop.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeDrawer(); });
})();
