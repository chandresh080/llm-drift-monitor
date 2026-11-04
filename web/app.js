/* Drift Monitor dashboard logic. Plain JavaScript, no libraries. */
"use strict";

const ALPHA = 0.05;
const FETCH_TIMEOUT_MS = 45000;   /* free tier cold starts can take a while */
const WAKE_MAX_TRIES = 10;
const WAKE_WAIT_MS = 8000;
const $ = (id) => document.getElementById(id);

const state = {
  health: null, stats: null, latest: null,
  results: [], alerts: [],
  awake: false,
};

/* ---------- small helpers ---------- */

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

function cellP(row, key) {
  const raw = Number(row[key]);
  if (!isFinite(raw)) return "";
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

/* Turn a stored alert message into something a person can read. */
function cleanAlertMessage(msg) {
  let m = String(msg || "");
  const m1 = m.match(/Significant tests: \[(.*?)]/);
  if (m1) {
    const tests = m1[1].replace(/['"]/g, "").trim();
    return "Tests that fired: " + (tests ? tests : "none") + ".";
  }
  return m;
}

function banner(text) {
  const el = $("banner");
  el.hidden = !text;
  el.textContent = text || "";
  if (text) {
    clearTimeout(banner._t);
    banner._t = setTimeout(() => { el.hidden = true; }, 8000);
  }
}

/* ---------- health pill ---------- */

function setPill(cls, text) {
  const pill = $("health-pill");
  pill.className = "pill " + cls;
  pill.innerHTML = `<span class="dot"></span>${text}`;
}

/* ---------- rendering ---------- */

function renderHeadlineHealth() {
  if (!state.awake) {
    setPill("pill-wake", "connecting");
    return;
  }
  const h = state.health;
  if (h && h.status === "ok") {
    setPill("pill-ok", "online");
  } else {
    setPill("pill-bad", "degraded");
  }
}

function renderKpis() {
  /* Drift status */
  const l = state.latest;
  const driftEl = $("kpi-drift"), driftSub = $("kpi-drift-sub");
  driftEl.classList.remove("kpi-ok", "kpi-bad");
  if (!state.awake) {
    driftEl.textContent = "...";
    driftSub.textContent = "connecting to the service";
  } else if (!l || l.drift_detected === null || l.drift_detected === undefined) {
    driftEl.textContent = "Pending";
    driftSub.textContent = "not enough stored responses yet";
  } else if (Number(l.drift_detected) === 1) {
    driftEl.textContent = "Drift detected";
    driftEl.classList.add("kpi-bad");
    driftSub.textContent = "the latest cycle differs from the baseline";
  } else {
    driftEl.textContent = "Stable";
    driftEl.classList.add("kpi-ok");
    driftSub.textContent = "the latest cycle matches the baseline";
  }

  /* Tests that fired */
  const flagged = (l && l.flagged_tests) || [];
  const flaggedEl = $("kpi-flagged");
  flaggedEl.textContent = flagged.length ? flagged.join(", ") : "None";
  flaggedEl.classList.toggle("kpi-flag", flagged.length > 0);

  /* Responses stored: exact count from /stats */
  const count = (state.stats && state.stats.probe_runs);
  $("kpi-runs").textContent = (state.awake && count !== null && count !== undefined)
    ? String(count) : "...";

  /* Adapter */
  $("kpi-provider").textContent = (state.awake && state.health && state.health.provider)
    ? state.health.provider : "...";
}

function renderChart() {
  const wrap = $("pval-chart");
  const results = state.results.slice().reverse(); /* oldest to newest */
  if (!results.length) {
    wrap.innerHTML = `<p class="empty">No results recorded yet.</p>`;
    return;
  }

  const N = results.length;
  const yOf = (p) => {
    const capped = Math.max(0, Math.min(Number(p) || 0, 0.10));
    return 84 - (capped / 0.10) * 72;
  };
  const xOf = (i) => ((i / (N - 1 || 1)) * 708 + 6).toFixed(1);

  const series = [
    { key: "ks_pvalue", title: "Response length (KS test)" },
    { key: "proportion_z_pvalue", title: "Share of empty responses (z test)" },
    { key: "mmd_pvalue", title: "Response semantics (MMD test)" },
  ];

  let html = "";
  for (const s of series) {
    const pts = results.map((r, i) => `${xOf(i)},${yOf(r[s.key])}`).join(" ");
    const dots = results
      .map((r, i) => ({ x: xOf(i), p: r[s.key] }))
      .filter((d) => Number(d.p) < ALPHA)
      .map((d) => `<circle cx="${d.x}" cy="${yOf(d.p)}" r="3.4" fill="#B34A1E" stroke="#FFFFFF" stroke-width="1.5"/>`)
      .join("");

    html += `
      <p class="chart-title">${escapeHtml(s.title)}</p>
      <svg viewBox="0 0 720 92" width="100%" height="92" role="img" preserveAspectRatio="none">
        <line x1="6" y1="${yOf(ALPHA)}" x2="714" y2="${yOf(ALPHA)}" stroke="#B9B9AF" stroke-width="1" stroke-dasharray="4 4" opacity="0.8"/>
        <text x="714" y="${yOf(ALPHA) - 5}" text-anchor="end" font-size="10.5" fill="#9AA098" font-family="ui-monospace, monospace">0.05</text>
        <polyline points="${pts}" fill="none" stroke="#171917" stroke-width="1.7" stroke-linejoin="round" stroke-linecap="round" opacity="0.85"/>
        ${dots}
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
    const fired = parseFlagged(r);
    const firedHtml = fired.length
      ? fired.map((f) => `<b>${escapeHtml(f)}</b>`).join(", ")
      : `<span class="none">none</span>`;
    return `<tr>
      <td class="ts">${fmtTime(r.timestamp)}</td>
      <td class="num">${cellP(r, "ks_pvalue")}</td>
      <td class="num">${cellP(r, "proportion_z_pvalue")}</td>
      <td class="num">${cellP(r, "mmd_pvalue")}</td>
      <td class="flag-cell">${firedHtml}</td>
      <td><span class="verdict ${drift ? "verdict-drift" : "verdict-ok"}">${drift ? "changed" : "unchanged"}</span></td>
    </tr>`;
  }).join("");
}

function renderAlerts() {
  const list = $("alerts-list");
  const rows = state.alerts.slice(0, 8);
  if (!rows.length) {
    list.innerHTML = `<li class="empty">No alerts recorded yet.</li>`;
    return;
  }
  list.innerHTML = rows.map((a) => {
    const sev = String(a.severity || "medium").toLowerCase();
    return `<li>
      <div class="alert-top">
        <b>${escapeHtml(a.alert_type === "quality_degradation" ? "Quality drop" : "Drift")}</b>
        <span class="sev sev-${escapeHtml(sev)}">${escapeHtml(sev)}</span>
      </div>
      <div class="alert-msg">${escapeHtml(cleanAlertMessage(a.message))}</div>
    </li>`;
  }).join("");
}

function renderAll() {
  /* Each panel renders on its own: a failure in one never blocks the others,
     and placeholders are replaced as soon as their data arrives. */
  for (const fn of [renderHeadlineHealth, renderKpis, renderChart, renderTable, renderAlerts]) {
    try { fn(); } catch (err) { console.error("render failed:", fn.name, err); }
  }
}

/* ---------- data loading ---------- */

async function fetchJson(path, options = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(path, { ...options, signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadAll() {
  /* All endpoints are queried at once: on a remote host each round trip
     costs latency, and no panel needs to wait for another. */
  const [health, stats, latest, results, alerts] = await Promise.all([
    fetchJson("/health"),
    fetchJson("/stats").catch(() => null),
    fetchJson("/drift/latest").catch(() => null),
    fetchJson("/drift/results?limit=20").catch(() => []),
    fetchJson("/alerts?limit=10").catch(() => []),
  ]);

  state.health = health;
  state.stats = stats;
  state.latest = latest;
  state.results = results || [];
  state.alerts = alerts || [];
  state.awake = true;
  renderAll();
  return true;
}

/* One wake loop for the whole page; every caller shares it. */
let wakePromise = null;

function ensureAwake() {
  if (state.awake) return Promise.resolve(true);
  if (wakePromise) return wakePromise;
  wakePromise = (async () => {
    banner("The free hosting plan pauses this service between visits. Waking it up now; this takes up to a minute.");
    for (let i = 0; i < WAKE_MAX_TRIES; i++) {
      try {
        if (await loadAll()) {
          banner("Connected. Panels below show the stored results.");
          return true;
        }
      } catch (err) {
        state.awake = false;   /* sleeping instance; keep placeholders, never fake numbers */
      }
      await new Promise((r) => setTimeout(r, WAKE_WAIT_MS));
    }
    banner("Still waking after a couple of minutes. Refresh the page or try again shortly.");
    return false;
  })();
  wakePromise.finally(() => { wakePromise = null; });
  return wakePromise;
}

/* ---------- run a check ---------- */

let runInFlight = false;

async function runCheck() {
  if (runInFlight) return;
  runInFlight = true;
  const btn = $("run-btn"), note = $("run-note");
  btn.disabled = true;
  btn.textContent = "Running...";
  try {
    if (!await ensureAwake()) {
      note.textContent = "The service did not come up in time. Try again in a minute.";
      return;
    }

    const started = await fetchJson("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const runId = started.run_id;
    note.textContent = "Running. Ten responses are collected, which takes up to a minute.";

    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) continue;

      if (st.status === "completed") {
        /* The verdict lives inside the monitoring result payload. */
        const r = st.result || {};
        if (r.tests_run) {
          const inner = r.results || {};
          const fired = Array.isArray(inner.bh_significant_tests)
            ? inner.bh_significant_tests : [];
          /* Read the final verdict back from the stored results so the note
             always matches the panels above. */
          const latest = await fetchJson("/drift/latest").catch(() => null);
          if (latest) { state.latest = latest; renderKpis(); }
          const drifted = latest
            ? Number(latest.drift_detected) === 1
            : Number(inner.overall_drift_detected) === 1;
          note.textContent = drifted
            ? "Drift detected in this cycle. The panels above show which tests fired."
            : "Cycle complete. No significant differences from the baseline.";
          if (drifted && fired.length) {
            banner("Drift detected: " + fired.join(", ") + " fired at alpha 0.05.");
          }
        } else {
          note.textContent = "Cycle complete. Ten responses stored. Tests begin once 60 responses are stored.";
        }
        await loadAll();
        return;
      }
      if (st.status === "failed") {
        note.textContent = "That run failed. Wait a moment and try again.";
        banner("The last run did not finish: " + (st.error || "unknown issue"));
        return;
      }
    }
    note.textContent = "The run is still going in the background. Panels update when it finishes.";
  } catch (err) {
    banner("Could not start a check: " + err.message);
    note.textContent = "No new responses were recorded.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Run a check";
    runInFlight = false;
  }
}

/* ---------- start ---------- */

$("run-btn").addEventListener("click", runCheck);
ensureAwake();
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) loadAll();
}, 60000);
