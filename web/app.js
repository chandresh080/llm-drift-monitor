/* Drift Monitor dashboard logic. Plain JavaScript, no libraries. */
"use strict";

const ALPHA = 0.05;
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
    return "The tests that fired: " + (tests ? tests : "none") + ".";
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
    setPill("pill-wake", "waking up, one moment");
    return;
  }
  const h = state.health;
  if (h && h.status === "ok") {
    setPill("pill-ok", "service online");
  } else {
    setPill("pill-bad", "service in trouble");
  }
}

function renderKpis() {
  /* Drift status */
  const l = state.latest;
  const driftEl = $("kpi-drift"), driftSub = $("kpi-drift-sub");
  driftEl.classList.remove("kpi-ok", "kpi-bad");
  if (!l || l.drift_detected === null || l.drift_detected === undefined) {
    driftEl.textContent = "Not enough data";
    driftSub.textContent = "a few more checks and this fills in";
  } else if (Number(l.drift_detected) === 1) {
    driftEl.textContent = "Drift seen";
    driftEl.classList.add("kpi-bad");
    driftSub.textContent = "worth a second look. Alerts below say which tests fired.";
  } else {
    driftEl.textContent = "Looks stable";
    driftEl.classList.add("kpi-ok");
    driftSub.textContent = "newest answers match the saved baseline";
  }

  /* Tests that fired */
  const flagged = (l && l.flagged_tests) || [];
  const flaggedEl = $("kpi-flagged");
  flaggedEl.textContent = flagged.length ? flagged.join(" and ") : "None";
  flaggedEl.classList.toggle("kpi-flag", flagged.length > 0);
  $("kpi-flagged-sub").textContent = state.awake
    ? "next check can change this"
    : "";

  /* Answers saved: exact count from /stats */
  const count = (state.stats && state.stats.probe_runs);
  $("kpi-runs").textContent = (count !== null && count !== undefined) ? String(count) : "0";
  $("kpi-runs-sub").textContent = state.awake
    ? "each check adds 10 more"
    : "counts appear once the service is awake";

  /* Adapter */
  $("kpi-provider").textContent = (state.health && state.health.provider) || "unknown";
}

function renderChart() {
  const wrap = $("pval-chart");
  const results = state.results.slice().reverse(); /* oldest to newest */
  if (!results.length) {
    wrap.innerHTML = `<p class="empty">No check results yet. Press "Run a check now" above, a few times if needed.</p>`;
    return;
  }

  const N = results.length;
  const yOf = (p) => {
    const capped = Math.max(0, Math.min(Number(p) || 0, 0.10));
    return 84 - (capped / 0.10) * 72;
  };
  const xOf = (i) => ((i / (N - 1 || 1)) * 708 + 6).toFixed(1);

  const series = [
    { key: "ks_pvalue", title: "Answer length (KS test)" },
    { key: "proportion_z_pvalue", title: "Share of empty answers (z test)" },
    { key: "mmd_pvalue", title: "What answers mean (embedding test)" },
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
      : `<span class="none">nothing</span>`;
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
    list.innerHTML = `<li class="empty">Nothing has fired yet. Alerts appear here when a check finds a real change.</li>`;
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
  renderHeadlineHealth();
  renderKpis();
  renderChart();
  renderTable();
  renderAlerts();
}

/* ---------- data loading (with wake-up handling) ---------- */

const WAKE_TRIES = 12;
const WAKE_WAIT_MS = 8000;

async function fetchJson(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(`HTTP ${res.status} on ${path}`);
  return res.json();
}

async function loadAll() {
  const pillWasAwake = state.awake;
  try {
    /* Health first: it is the cheapest call and reveals whether we are awake. */
    const health = await fetchJson("/health");
    state.health = health;
    state.awake = true;
    if (!pillWasAwake) banner("Service is awake. Loading the latest numbers.");

    /* Everything else can load independently; missing pieces render as pending. */
    const [stats, latest, results, alerts] = await Promise.all([
      fetchJson("/stats").catch(() => null),
      fetchJson("/drift/latest").catch(() => null),
      fetchJson("/drift/results?limit=20").catch(() => []),
      fetchJson("/alerts?limit=10").catch(() => []),
    ]);
    state.stats = stats;
    state.latest = latest;
    state.results = results || [];
    state.alerts = alerts || [];
    banner("");
    renderAll();
    return true;
  } catch (err) {
    /* Probably asleep. Say so, wait, try again. Do not show fake numbers. */
    state.awake = false;
    state.stats = null;
    state.latest = null;
    renderHeadlineHealth();
    banner("The demo host is waking up. This takes about a minute on the free plan. The page keeps trying by itself.");
    await new Promise((r) => setTimeout(r, WAKE_WAIT_MS));
    return false;
  }
}

async function waitForService() {
  for (let i = 0; i < WAKE_TRIES; i++) {
    const ok = await loadAll();
    if (ok) return;
  }
  banner("Still waking after a couple of minutes. Refresh the page, or come back shortly.");
}

/* ---------- run a check ---------- */

let runInFlight = false;

async function runCheck() {
  if (runInFlight) return;
  runInFlight = true;
  const btn = $("run-btn"), note = $("run-note");
  btn.disabled = true;
  btn.textContent = "Checking...";
  try {
    // Make sure we are awake before starting a run.
    if (!state.awake) await waitForService();
    const started = await fetchJson("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const runId = started.run_id;
    note.textContent = "Running. This takes up to a minute on the free plan.";
    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) continue;
      if (st.status === "completed") {
        const r = st.result || {};
        if (r.tests_run) {
          note.textContent = r.overall_drift_detected
            ? "Done. The answers changed. See the panel above for which tests fired."
            : "Done. Answers look steady, nothing fired.";
          banner("Check finished. Panels updated.");
        } else {
          note.textContent = "Done. 10 answers stored. At least 60 saved answers are needed for the tests.";
          banner("10 answers stored. Run a few more checks to fill the baseline.");
        }
        break;
      }
      if (st.status === "failed") {
        note.textContent = "That run failed. Wait a moment and try again.";
        banner("The last run did not finish: " + (st.error || "unknown issue"));
        break;
      }
    }
  } catch (err) {
    banner("Could not start a check: " + err.message);
    note.textContent = "No new answers were added.";
  }
  btn.disabled = false;
  btn.textContent = "Run a check now";
  runInFlight = false;
  loadAll();
}

/* ---------- start ---------- */

$("run-btn").addEventListener("click", runCheck);
waitForService();
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) loadAll();
}, 60000);
