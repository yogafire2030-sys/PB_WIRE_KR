/* Factor Factory front-end. 외부 데이터는 textContent로만 삽입(XSS 방지). */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const D = JSON.parse($("#payload").textContent || "null");
  const csrfMeta = $('meta[name="csrf-token"]');
  const CSRF = csrfMeta ? csrfMeta.content : "";
  const css = getComputedStyle(document.documentElement);
  const C = (v) => css.getPropertyValue(v).trim();
  const STATUS = { S: "채택", I: "비유의", P: "상관제거", N: "분산없음" };
  const PALETTE = ["#2fd4b4", "#d9b26f", "#6aa8ff", "#ff7a8a", "#b18cff", "#f5b84a", "#5fd38a", "#ff9e64"];
  const charts = [];

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };
  const fmt = (v, d = 3) => (v === null || v === undefined ? "—" : Number(v).toFixed(d));
  const fmtP = (v) => (v === null || v === undefined ? "—" : v < 0.0001 ? "<0.0001" : Number(v).toFixed(4));

  /* ---------- 업데이트 (로컬 앱에서만; 정적 사이트엔 버튼이 없음) ---------- */
  const statusEl = $("#status");
  const btn = $("#btn-update");
  if (btn) {
  let pollTimer = null;

  function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "pill" + (kind ? " " + kind : "");
  }

  function readSettings() {
    const f = $("#settings");
    const out = {};
    for (const name of ["window", "alpha", "mode", "correction", "corr_max", "halflife", "nw_lags", "history"]) {
      out[name] = f.elements[name].value;
    }
    out.horizon = f.elements.horizon.value;
    out.fc_window = f.elements.fc_window.value;
    return { settings: out, demo: f.elements.demo.checked, all: f.elements.all.checked };
  }

  async function poll() {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      const s = await r.json();
      if (s.running) {
        setStatus(s.message, "run");
        pollTimer = setTimeout(poll, 1200);
        return;
      }
      btn.disabled = false;
      if (s.error) { setStatus(s.error, "err"); return; }
      setStatus(s.message || "완료", "ok");
      setTimeout(() => location.reload(), 400);
    } catch (e) {
      btn.disabled = false;
      setStatus("상태 확인 실패", "err");
    }
  }

  btn.addEventListener("click", async () => {
    const target = new URLSearchParams(location.search).get("target") || (D && D.target) || "KOSPI";
    const body = { target, ...readSettings() };
    btn.disabled = true;
    setStatus("요청 중…", "run");
    try {
      const r = await fetch("/api/update", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF },
        body: JSON.stringify(body),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok && r.status !== 409) { btn.disabled = false; setStatus(j.error || "요청 실패", "err"); return; }
      clearTimeout(pollTimer);
      poll();
    } catch (e) {
      btn.disabled = false;
      setStatus("서버에 연결할 수 없습니다", "err");
    }
  });

  // 페이지 진입 시 진행 중인 작업이 있으면 이어서 표시
  fetch("/api/status", { cache: "no-store" }).then((r) => r.json()).then((s) => {
    if (s.running) { btn.disabled = true; poll(); }
  }).catch(() => {});
  }

  if (!D) return;

  /* ---------- 전망 확률 마커 ---------- */
  const pbar = $("#pbar");
  if (pbar) {
    const p = Math.min(Math.max(parseFloat(pbar.dataset.p) || 0.5, 0), 1);
    pbar.querySelector("i").style.left = (p * 100).toFixed(1) + "%";
  }

  /* ---------- 오늘 움직임 분해 (CSSOM으로만 폭 지정 → CSP 호환) ---------- */
  const dc = D.insight && D.insight.decomp;
  const dcBox = $("#decomp");
  if (dc && dcBox) {
    const rows = [
      ...dc.items.map((i) => [i.name, i.v, ""]),
      ["평균 수익률(상수)", dc.base, ""],
      ["설명 안 된 부분", dc.resid, ""],
      ["실제 등락", dc.actual, "total"],
    ];
    const maxAbs = Math.max(0.05, ...rows.map((r) => Math.abs(r[1] || 0)));
    const f2 = document.createDocumentFragment();
    for (const [label, v, cls] of rows) {
      const row = el("div", "dc-row " + cls);
      const track = el("div", "dc-track");
      const bar = el("div", "dc-bar " + (v >= 0 ? "pos" : "neg"));
      const w = (Math.abs(v) / maxAbs) * 50;
      bar.style.width = w + "%";
      bar.style.left = v >= 0 ? "50%" : 50 - w + "%";
      track.append(bar);
      row.append(el("span", "lbl", label), track, el("span", "v", (v >= 0 ? "+" : "") + v.toFixed(2) + "%"));
      f2.append(row);
    }
    dcBox.append(f2);
  }

  /* ---------- 팩터 테이블 ---------- */
  const tbody = $("#factor-table tbody");
  const maxW = Math.max(0.0001, ...D.rows.map((r) => r.w_ewma || 0));
  const frag = document.createDocumentFragment();
  for (const r of D.rows) {
    const tr = el("tr", r.status === "S" || (r.w_ewma || 0) > 0.01 ? "" : "dim");
    const name = el("td");
    name.append(el("span", "", r.name), el("span", "grp", r.group));
    const st = el("td");
    st.append(el("span", "badge " + r.status, STATUS[r.status] || r.status));
    const wb = el("td");
    const bar = el("div", "wbar");
    const track = el("div", "track");
    const fill = el("div", "fill" + ((r.b_ewma || 0) < 0 ? " neg" : ""));
    fill.style.width = Math.round(((r.w_ewma || 0) / maxW) * 100) + "%";
    track.append(fill);
    bar.append(track, el("span", "v", ((r.w_ewma || 0) * 100).toFixed(1) + "%"));
    wb.append(bar);
    const beta = el("td", "num " + (r.beta > 0 ? "pos" : r.beta < 0 ? "neg" : ""), fmt(r.beta));
    tr.append(name, st, beta, el("td", "num", fmt(r.t, 2)), el("td", "num", fmtP(r.p)),
      el("td", "num", r.w_raw ? (r.w_raw * 100).toFixed(1) + "%" : "—"), wb,
      el("td", "num", ((r.freq || 0) * 100).toFixed(0) + "%"));
    frag.append(tr);
  }
  tbody.append(frag);

  /* ---------- Chart.js 기본값 ---------- */
  if (window.Chart) {
    Chart.defaults.color = C("--muted");
    Chart.defaults.borderColor = "rgba(34,48,79,.6)";
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 11;
    Chart.defaults.animation = false;
  }
  const shortDates = D.series.dates.map((d) => d.slice(2));
  const baseOpts = (extra = {}) => ({
    responsive: true, maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { labels: { boxWidth: 10, boxHeight: 10 } } },
    elements: { point: { radius: 0 }, line: { borderWidth: 1.6, tension: 0.15 } },
    scales: { x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } } },
    ...extra,
  });

  /* ---------- t값 추이 ---------- */
  let tChart = null;
  function showT(key) {
    if (!window.Chart) return;
    const crit = D.settings.correction === "t3" ? 3 : 1.96;
    const data = {
      labels: shortDates,
      datasets: [
        { label: D.names[key] + " t값", data: D.tline[key], borderColor: C("--accent"), spanGaps: true },
        { label: "+" + crit, data: shortDates.map(() => crit), borderColor: "rgba(217,178,111,.6)", borderDash: [4, 4], borderWidth: 1 },
        { label: "−" + crit, data: shortDates.map(() => -crit), borderColor: "rgba(217,178,111,.6)", borderDash: [4, 4], borderWidth: 1 },
      ],
    };
    if (tChart) { tChart.data = data; tChart.update(); return; }
    tChart = new Chart($("#chart-t"), { type: "line", data, options: baseOpts() });
    charts.push(tChart);
  }

  /* ---------- 히트맵 (CSSOM으로만 색 지정) ---------- */
  const hm = $("#heatmap");
  const nCols = D.heat.dates.length;
  const pos = C("--pos"), neg = C("--neg");
  const hex2rgb = (h) => { const n = parseInt(h.replace("#", ""), 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; };
  const [pr, pg, pb] = hex2rgb(pos), [nr, ng, nb] = hex2rgb(neg);
  const hmFrag = document.createDocumentFragment();
  let firstKey = null;
  for (const key of D.order) {
    const row = el("div", "hm-row");
    row.dataset.key = key;
    row.title = D.names[key];
    const cells = el("div", "hm-cells");
    cells.style.gridTemplateColumns = `repeat(${nCols}, minmax(5px, 1fr))`;
    const ts = D.heat.t[key], ss = D.heat.s[key] || "";
    for (let i = 0; i < nCols; i++) {
      const c = el("div", "hm-cell" + (ss[i] === "S" ? " sel" : ""));
      const t = ts[i];
      if (t !== null && t !== undefined) {
        const a = Math.min(Math.abs(t) / 4, 1) * 0.9 + 0.05;
        c.style.background = t >= 0 ? `rgba(${pr},${pg},${pb},${a})` : `rgba(${nr},${ng},${nb},${a})`;
        c.title = `${D.heat.dates[i]}  t=${t.toFixed(2)}  ${STATUS[ss[i]] || ""}`;
      }
      cells.append(c);
    }
    row.append(el("div", "hm-label", D.names[key]), cells);
    hmFrag.append(row);
    if (!firstKey) firstKey = key;
  }
  const axis = el("div", "hm-axis");
  axis.append(el("span", "", D.heat.dates[0]), el("span", "", D.heat.dates[nCols - 1]));
  hmFrag.append(axis);
  hm.append(hmFrag);
  hm.addEventListener("click", (ev) => {
    const row = ev.target.closest(".hm-row");
    if (!row) return;
    hm.querySelectorAll(".hm-row.on").forEach((r) => r.classList.remove("on"));
    row.classList.add("on");
    showT(row.dataset.key);
  });
  if (firstKey) { hm.querySelector(".hm-row").classList.add("on"); showT(firstKey); }

  if (!window.Chart) return;

  /* ---------- 전망 vs 실제 ---------- */
  const F = D.forecast;
  if (F && F.series && $("#chart-fc")) {
    const fd = F.series.dates.map((d) => d.slice(2));
    charts.push(new Chart($("#chart-fc"), {
      data: {
        labels: fd,
        datasets: [
          { type: "line", label: `예상 ${F.horizon}일 수익률(%)`, data: F.series.pred, borderColor: C("--gold"), yAxisID: "y", order: 0 },
          { type: "bar", label: `실제 ${F.horizon}일 수익률(%) · 그날 기준 이후 ${F.horizon}일`, data: F.series.real, yAxisID: "y",
            backgroundColor: F.series.real.map((v) => (v === null ? "transparent" : v >= 0 ? "rgba(47,212,180,.45)" : "rgba(255,122,138,.45)")),
            barPercentage: 1, categoryPercentage: 1, order: 1 },
        ],
      },
      options: baseOpts({
        scales: {
          x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
          y: { ticks: { callback: (v) => v + "%" } },
        },
      }),
    }));
  }

  /* ---------- 가중치 추이 (누적 영역) ---------- */
  const top = D.order.slice(0, 8);
  charts.push(new Chart($("#chart-w"), {
    type: "line",
    data: {
      labels: shortDates,
      datasets: top.map((k, i) => ({
        label: D.names[k], data: D.weights[k].map((v) => (v === null ? null : v * 100)),
        borderColor: PALETTE[i], backgroundColor: PALETTE[i] + "55", fill: true, borderWidth: 1,
      })),
    },
    options: baseOpts({
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
        y: { stacked: true, min: 0, max: 100, ticks: { callback: (v) => v + "%" } },
      },
    }),
  }));

  /* ---------- 진단 ---------- */
  charts.push(new Chart($("#chart-r2"), {
    data: {
      labels: shortDates,
      datasets: [
        { type: "line", label: "조정 R²", data: D.series.adj_r2, borderColor: C("--accent"), yAxisID: "y" },
        { type: "bar", label: "유의 팩터 수", data: D.series.k, backgroundColor: "rgba(217,178,111,.35)", yAxisID: "y1", barPercentage: 1, categoryPercentage: 1 },
      ],
    },
    options: baseOpts({
      scales: {
        x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
        y: { position: "left", min: 0 },
        y1: { position: "right", min: 0, grid: { display: false }, ticks: { precision: 0 } },
      },
    }),
  }));

  // 페이지 이탈 시 차트 리소스 해제
  window.addEventListener("pagehide", () => { charts.forEach((c) => c.destroy()); charts.length = 0; });
})();
