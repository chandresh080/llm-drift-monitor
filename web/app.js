/* LLM Drift Monitor — dashboard logic. Vanilla JS, no dependencies. */
"use strict";

const ALPHA = 0.05;
const $ = (id) => document.getElementById(id);

const state = { health: null, latest: null, results: [], runs: [], alerts: [] };

/* ---------- helpers ---------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return escapeHtml(iso || "");
  const date = d.toLocaleDateString(undefined, { month: "short", day: "2-digit" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `<span class="d">${date}</span> <span class="t">${time}</span>`;
}

/** BH-adjusted p-value cell; significant values render bold in .sig-cell b color. */
function cellP(row, key) {
  const raw = Number(row[key]);
  if (!isFinite(raw)) return "&mdash;";
  const sig = row[key.replace("_pvalue", "_significant")] === 1 || raw < ALPHA;
  return sig ? `<b>${raw.toFixed(3)}</b>` : raw.toFixed(3);
}

function parseFlagged(row) {
  let raw = row.bh_significant_tests;
  if (typeof raw === "string") {
    try { raw = JSON.parse(raw); } catch { raw = []; }
  }
  return Array.isArray(raw) ? raw : [];
}

/* ---------- banner ---------- */

function banner(text, sticky = false) {
  const el = $("banner");
  el.hidden = !text;
  el.textContent = text;
  if (!sticky && text) setTimeout(() => { el.hidden = true; }, 6000);
}

/* ---------- rendering ---------- */

function renderHealth() {
  const pill = $("health-pill");
  const h = state.health;
  pill.classList.remove("pill-ok", "pill-bad", "pill-muted");
  if (!h) {
    pill.classList.add("pill-muted");
    pill.innerHTML = `<span class="dot"></span>DB unreachable`;
    return;
  }
  const model = h.provider || "unknown";
  if (h.status === "ok") {
    pill.classList.add("pill-ok");
    pill.innerHTML = `<span class="dot"></span>DB ok &middot; ${escapeHtml(model)}`;
  } else {
    pill.classList.add("pill-bad");
    pill.innerHTML = `<span class="dot"></span>DB degraded &middot; ${escapeHtml(model)}`;
  }
}

function renderKpis() {
  const l = state.latest;
  const drift = $("kpi-drift"), driftSub = $("kpi-drift-sub");
  drift.classList.remove("kpi-ok", "kpi-bad");

  if (!l || l.drift_detected === null || l.drift_detected === undefined) {
    drift.textContent = "No verdict yet";
    driftSub.textContent = "run a cycle to collect probes and tests";
  } else if (Number(l.drift_detected) === 1) {
    drift.textContent = "Drift flagged";
    drift.classList.add("kpi-bad");
    driftSub.textContent = "investigate the response distributions";
  } else {
    drift.textContent = "No drift";
    drift.classList.add("kpi-ok");
    driftSub.textContent = "current window matches baseline";
  }

  const flagged = (l && l.flagged_tests) || [];
  const flaggedEl = $("kpi-flagged");
  flaggedEl.textContent = flagged.length ? flagged.join(" + ") : "None";
  flaggedEl.classList.toggle("kpi-flag", flagged.length > 0);

  const runs = state.runs || [];
  $("kpi-runs").textContent = runs.length >= 100 ? "100+" : String(runs.length || "0");
  const latestTs = runs[0] && runs[0].timestamp;
  $("kpi-runs-sub").textContent = latestTs
    ? `latest probe run ${new Date(latestTs).toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })}`
    : "no probes yet";

  $("kpi-provider").textContent = (state.health && state.health.provider) || "—";
}

function renderChart() {
  const wrap = $("pval-chart");
  const results = state.results.slice().reverse(); // oldest -> newest
  if (!results.length) {
    wrap.innerHTML = `<p class="empty">No test results yet. Trigger a monitoring cycle.</p>`;
    return;
  }

  const W = 720, MAXY = 84, N = results.length;
  // p-values plotted on 0..0.10; alpha line at 0.05
  const yOf = (p) => {
    const capped = Math.max(0, Math.min(Number(p) || 0, 0.10));
    return MAXY - (capped / 0.10) * (MAXY - 12);
  };

  const series = [
    { key: "ks_pvalue", label: "KS — response length" },
    { key: "proportion_z_pvalue", label: "Z — validity rate" },
    { key: "mmd_pvalue", label: "MMD — embeddings" },
  ];

  let html = "";
  for (const s of series) {
    const xOf = (i) => ((i / (N - 1 || 1)) * 708 + 6).toFixed(1);
    const pts = results.map((r, i) => `${xOf(i)},${yOf(r[s.key])}`).join(" ");
    const sigMarks = results
      .map((r, i) => ({ x: xOf(i), p: r[s.key] }))
      .filter((d) => Number(d.p) < ALPHA)
      .map((d) => `<circle cx="${d.x}" cy="${yOf(d.p)}" r="3.4" fill="#B34A1E" stroke="#FFFFFF" stroke-width="1.5"/>`)
      .join("");

    html += `
      <p class="chart-title">${escapeHtml(s.label)}</p>
      <svg viewBox="0 0 720 ${MAXY + 8}" width="100%" height="${MAXY + 8}" role="img" preserveAspectRatio="none">
        <line x1="6" y1="${yOf(ALPHA)}" x2="714" y2="${yOf(ALPHA)}" stroke="#B9B9AF" stroke-width="1" stroke-dasharray="4 4" opacity="0.8"/>
        <text x="714" y="${yOf(ALPHA) - 5}" text-anchor="end" font-size="10.5" fill="#9AA098" font-family="ui-monospace, monospace">&#945; = .05</text>
        <polyline points="${pts}" fill="none" stroke="#171917" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>
        ${sigMarks}
      </svg>`;
  }
  wrap.innerHTML = html;
}

function renderTable() {
  const rows = state.results.slice(0, 12);
  const table = $("results-table");
  if (!rows.length) {
    table.hidden = true;
    return;
  }
  table.hidden = false;
  table.querySelector("tbody").innerHTML = rows.map((r) => {
    const drift = Number(r.overall_drift_detected) === 1;
    const flagged = parseFlagged(r);
    const flaggedHtml = flagged.length
      ? flagged.map((f) => `<b>${escapeHtml(f)}</b>`).join(" + ")
      : `<span class="none">&mdash;</span>`;
    return `<tr>
      <td class="ts">${fmtTime(r.timestamp)}</td>
      <td class="num">${cellP(r, "ks_pvalue")}</td>
      <td class="num">${cellP(r, "proportion_z_pvalue")}</td>
      <td class="num">${cellP(r, "mmd_pvalue")}</td>
      <td class="flag-cell">${flaggedHtml}</td>
      <td><span class="verdict ${drift ? "verdict-drift" : "verdict-ok"}">${drift ? "drift" : "stable"}</span></td>
    </tr>`;
  }).join("");
}

function renderAlerts() {
  const list = $("alerts-list");
  const rows = state.alerts.slice(0, 8);
  if (!rows.length) {
    list.innerHTML = `<li class="empty">No alerts on record.</li>`;
    return;
  }
  list.innerHTML = rows.map((a) => {
    const sev = String(a.severity || "low").toLowerCase();
    return `<li>
      <div class="alert-top">
        <b>${escapeHtml(a.alert_type || "alert")}</b>
        <span class="sev sev-${escapeHtml(sev)}">${escapeHtml(sev)}</span>
      </div>
      <div class="alert-msg">${escapeHtml(a.message || "")}</div>
    </li>`;
  }).join("");
}

/* ---------- data loading ---------- */

function renderAll() {
  renderHealth();
  renderKpis();
  renderChart();
  renderTable();
  renderAlerts();
}

async function api(path, options = {}, tries = 3) {
  // The free-tier instance may sleep; the first request can take ~60s. Retry politely.
  for (let attempt = 1; attempt <= tries; attempt++) {
    try {
      const res = await fetch(path, options);
      if (!res.ok) throw new Error(`${path} &rarr; HTTP ${res.status}`);
      return await res.json();
    } catch (err) {
      if (attempt === tries) throw err;
      banner(`Service is waking up (free tier). Retrying&hellip; (${attempt}/${tries})`, true);
      await new Promise((r) => setTimeout(r, 8000));
    }
  }
}

async function loadAll() {
  try {
    const [health, latest, results, runs, alerts] = await Promise.all([
      api("/health"),
      api("/drift/latest").catch(() => null),
      api("/drift/results?limit=20").catch(() => []),
      api("/runs?limit=100").catch(() => []),
      api("/alerts?limit=10").catch(() => []),
    ]);
    state.health = health;
    state.latest = latest;
    state.results = results || [];
    state.runs = runs || [];
    state.alerts = alerts || [];
  } catch (err) {
    banner("Could not reach the API. The instance may be cold-starting — it will retry in a moment.");
  }
  renderAll();
}

/* ---------- run a monitoring cycle ---------- */

let running = false;

async function runCycle() {
  if (running) return;
  running = true;
  const btn = $("run-btn"), note = $("run-note");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    const started = await api("/runs", { method: "POST", headers: { "Content-Type": "application/json" } });
    const runId = started.run_id;
    note.textContent = `cycle ${runId.slice(0, 8)} — running probes…`;
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await api("/runs/" + runId);
      if (st.status === "completed") {
        const r = st.result || {};
        const flagged = (r.bh_significant_tests || []).join(" + ");
        note.textContent = r.tests_run
          ? `cycle complete — drift ${r.overall_drift_detected ? "flagged" : "not flagged"}${r.overall_drift_detected ? ", flagged: " + flagged : ""}`
          : "cycle complete — probes stored; tests need a full window (60+ runs).";
        banner("Monitoring cycle finished. Panels updated.");
        break;
      }
      if (st.status === "failed") {
        banner("Run failed: " + (st.error || "unknown error"));
        note.textContent = "last run failed.";
        break;
      }
    }
  } catch (err) {
    banner("Could not start a run: " + err.message);
    note.textContent = "no run — API unreachable.";
  }
  btn.disabled = false;
  btn.textContent = "Run monitoring cycle";
  running = false;
  loadAll();
}

/* ---------- boot ---------- */

$("run-btn").addEventListener("click", runCycle);
loadAll();
setInterval(() => {
  if (!running && document.visibilityState === "visible") loadAll();
}, 60000);
