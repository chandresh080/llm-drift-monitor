/* Drift Monitor live console.
   Vanilla JS, no dependencies; all numbers come from the REST API on the
   same origin. Nothing is computed client side except formatting. */

const state = {
  health: null,       // {status, provider, provider_status, db}
  config: null,       // /config payload
  stats: null,        // {probe_runs, test_results, alerts}
  latest: null,       // /drift/latest payload
  results: [],        // recent stored cycles
  alerts: [],         // recent persisted alerts
  comparison: null,   // /drift/comparison
  categories: null,   // /drift/categories
  evaluation: null,   // /evaluation
  awake: false,
  wakeStartedAt: null,
};

const WAKE_WAIT_MS = 5000;
const NUM_TESTS = 3;      // pooled family: KS, validity, MMD
const ALPHA = 0.05;       // only for display fallback; the API reports decisions

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return "unknown";
  return t.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtP(p) {
  if (p == null) return "n/a";
  return Number(p) < 0.001 ? "< 0.001" : Number(p).toFixed(3);
}

function banner(text) {
  const el = $("banner");
  el.hidden = false;
  el.textContent = text;
  clearTimeout(banner._t);
  banner._t = setTimeout(() => { el.hidden = true; }, 10000);
}

async function fetchJson(url, opts = {}) {
  const timer = new Promise((_, reject) =>
    setTimeout(() => reject(new Error("timeout")), 20000));
  const res = Promise.race([fetch(url, opts), timer]).then(async (resp) => {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return resp.json();
  });
  return await res;
}

/* ---------- health pill and banner ---------- */

function setPill(cls, text) {
  const pill = $("health-pill");
  pill.className = "pill";
  if (cls) pill.classList.add(cls);
  pill.setAttribute("data-status", text.split(" ")[0]);
  pill.innerHTML = `<span class="dot"></span>${escapeHtml(text)}`;
}

function renderHeadlineHealth() {
  if (!state.health) return;
  const allOk = state.health.status === "ok";
  const providerOk = state.health.provider_status === "ok";
  const dbOk = state.health.db === "ok";
  setPill("pill-ok",
    allOk ? "Service online" :
      (providerOk && dbOk ? "Service online" : "Service degraded"));
  if (!allOk) {
    banner(`Service is reachable but degraded: provider ${state.health.provider_status}, database ${state.health.db}. Results below reflect the stored history.`);
  }
}

/* ---------- tabs ---------- */

function initTabs() {
  const show = (view) => {
    $("tab-console").hidden = view !== "console";
    $("tab-evaluation").hidden = view !== "evaluation";
    $("tab-btn-console").classList.toggle("tab-active", view === "console");
    $("tab-btn-evaluation").classList.toggle("tab-active", view === "evaluation");
    if (view === "evaluation" && !state.evaluation) loadEvaluation();
  };
  $("tab-btn-console").addEventListener("click", () => show("console"));
  $("tab-btn-evaluation").addEventListener("click", () => show("evaluation"));
}

/* ---------- overview: KPIs and configuration ---------- */

function renderKpis() {
  const latest = state.latest;
  const driftEl = $("kpi-drift"), driftSub = $("kpi-drift-sub");
  if (!latest || latest.drift_detected == null) {
    driftEl.textContent = "No evaluation yet";
    driftSub.textContent = "run a check, or wait until tests have data";
  } else if (Number(latest.drift_detected) === 1) {
    driftEl.textContent = "Drift flagged";
    driftEl.className = "kpi-value kpi-alert";
    driftSub.textContent = `latest cycle (${fmtTime(latest.timestamp)}) differs statistically from the reference window`;
  } else {
    driftEl.textContent = "Stable";
    driftEl.className = "kpi-value kpi-ok";
    driftSub.textContent = `latest cycle (${fmtTime(latest.timestamp)}) matches the reference window statistically`;
    driftSub.title = "Stable means: no statistically significant difference was found between the latest cycle and the reference window. It does not mean the model is good.";
  }

  const fired = (latest && Array.isArray(latest.bh_significant_tests))
    ? latest.bh_significant_tests.length : 0;
  $("kpi-flagged").textContent = latest ? `${fired} / ${NUM_TESTS}` : "...";
  $("kpi-flagged").className = "kpi-value" + (fired > 0 ? " kpi-alert" : "");

  $("kpi-runs").textContent = state.stats ? Number(state.stats.probe_runs).toLocaleString() : "...";
  $("kpi-runs").className = "kpi-value";

  const provider = state.health ? state.health.provider : "...";
  $("kpi-provider").textContent = provider;
}

function renderConfig() {
  const c = state.config;
  if (!c) return;
  $("cfg-model").textContent = `${c.target_model} (${c.adapter_type})`;
  $("cfg-embedding").textContent = c.embedding_model;
  $("cfg-ref").textContent = `${c.reference_window} responses (5 complete cycles)`;
  $("cfg-cur").textContent = `${c.current_window} responses (1 cycle)`;
  $("cfg-panel").textContent =
    `v${c.prompt_panel_version}, ${c.prompt_panel_probes} prompts, ${c.probes_per_cycle} per cycle`;
  $("cfg-alpha").textContent = Number(c.alpha).toFixed(2)
    + " after Benjamini-Hochberg correction";
  $("cfg-persist").textContent =
    `${c.min_consecutive_cycles} consecutive flagged cycles before an alert fires`;
  const ts = state.latest && state.latest.timestamp;
  $("cfg-last-run").textContent = ts ? `${fmtTime(ts)} (${Number(state.latest.current_size)} responses vs ${Number(state.latest.reference_size)})` : "no cycle recorded yet";

  /* Keep the buttons note accurate for this deployment. */
  $("run-note").textContent = `Each run records ${c.current_window} responses. Statistical tests begin once ${c.tests_start_at_responses} responses are stored (one full reference window plus one current cycle).`;
}

/* ---------- detector cards and effect sizes ---------- */

function detectorCard(prefix, row, extra) {
  const flagged = row && row.significant === 1;
  $(prefix + "-decision").textContent =
    row == null ? "no data" : (flagged ? "Flagged" : "No flag");
  $(prefix + "-decision").className = "chip " + (flagged ? "chip-alert" : "chip-ok");
  $(prefix + "-p").textContent = row ? fmtP(row.p) : "n/a";
}

function renderDetectors() {
  const l = state.latest;
  if (!l) return;
  detectorCard("det-ks",
    { significant: l.ks_significant, p: l.ks_adjusted_pvalue });
  detectorCard("det-prop",
    { significant: l.proportion_z_significant, p: l.proportion_z_adjusted_pvalue });
  detectorCard("det-mmd",
    { significant: l.mmd_significant, p: l.mmd_adjusted_pvalue });
}

function renderEffects() {
  const l = state.latest || {};
  const set = (id, value) => {
    const el = $(id);
    if (value == null) {
      el.textContent = "not recorded";
      el.className = "effect-value effect-missing";
    } else {
      el.textContent = value;
      el.className = "effect-value";
    }
  };
  set("eff-length",
    l.length_shift_pct == null ? null :
      `${l.length_shift_pct > 0 ? "+" : ""}${l.length_shift_pct}%`);
  set("eff-centroid",
    l.centroid_distance == null ? null : Number(l.centroid_distance).toFixed(3));
  set("eff-refusal",
    (l.refusal_rate_reference != null && l.refusal_rate_current != null)
      ? `${l.refusal_rate_reference > 0 ? "+" : ""}${(100 * (l.refusal_rate_current - l.refusal_rate_reference)).toFixed(1)} pp`
      : null);
}

/* ---------- reference vs current comparison ---------- */

function responseCell(data) {
  if (!data || data.text == null) {
    return '<span class="muted">no stored answer</span>';
  }
  const chips = [];
  chips.push('<span class="meta-chip">' + data.length + " chars</span>");
  if (data.is_valid != null) {
    chips.push('<span class="meta-chip ' + (data.is_valid ? "chip-ok-line" : "chip-alert-line") + '">'
      + (data.is_valid ? "valid" : "invalid") + "</span>");
  }
  if (data.is_refusal === 1) {
    chips.push('<span class="meta-chip chip-alert-line">refusal marker</span>');
  }
  const snippet = escapeHtml(String(data.text).slice(0, 110))
    + (String(data.text).length > 110 ? " ..." : "");
  return `<code class="resp-snippet">${snippet}</code>
    <div class="resp-chips">${chips.join("")}</div>`;
}

function renderComparison() {
  const wrap = $("comparison-wrap");
  const c = state.comparison;
  if (c == null) {
    wrap.innerHTML = '<p class="muted">Comparison unavailable.</p>';
    return;
  }
  if (!c.ready) {
    wrap.innerHTML = `
      <div class="empty-state">
        <strong>Tests have not started yet.</strong><br>
        The monitor stores two kinds of windows: a reference window of
        ${c.reference_required} responses (the baseline) and a current window of
        ${c.current_required} (what is compared against it). Right now the service holds
        ${c.reference_stored} of ${c.reference_required} reference responses and
        ${c.current_stored} of ${c.current_required} current responses.
        Once ${c.tests_start_at_responses} responses are stored, the first cycle runs all
        three detectors and this panel shows the answers side by side. This is expected
        behaviour on a fresh deployment, not a fault.
      </div>`;
    return;
  }
  const rows = c.rows.map((r, i) => `
    <tr class="cmp-row" data-i="${i}">
      <td class="cmp-cat"><span class="chip chip-cat">${escapeHtml(r.category)}</span></td>
      <td class="cmp-prompt">${escapeHtml(r.prompt_text)}</td>
      <td class="cmp-resp">${responseCell(r.reference)}</td>
      <td class="cmp-resp">${responseCell(r.current)}</td>
    </tr>`).join("");
  wrap.innerHTML = `
    <table class="cmp-table">
      <thead>
        <tr>
          <th>Category</th><th>Prompt (matched panel)</th>
          <th>Reference response <span class="muted">(baseline window)</span></th>
          <th>Current response <span class="muted">(latest cycle)</span></th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
    <div id="cmp-detail"></div>
    <p class="panel-note">Click a row to read the complete responses. Reference shows
      the newest answer to each prompt that is still inside the baseline window; current
      shows the newest answer from the latest cycle.</p>`;

  const detail = wrap.querySelector("#cmp-detail");
  wrap.querySelectorAll(".cmp-row").forEach((tr) => {
    tr.addEventListener("click", () => {
      const r = c.rows[Number(tr.dataset.i)];
      detail.innerHTML = `
        <details open class="cmp-detail">
          <summary>${escapeHtml(r.prompt_text)} <span class="chip chip-cat">${escapeHtml(r.category)}</span></summary>
          <div class="cmp-detail-grid">
            <div><h4>Reference response</h4><code>${escapeHtml(r.reference.text)}</code></div>
            <div><h4>Current response</h4><code>${escapeHtml(r.current.text)}</code></div>
          </div>
        </details>`;
      detail.scrollIntoView({ block: "nearest" });
    });
  });
}

/* ---------- category localization ---------- */

function renderCategories() {
  const wrap = $("categories-wrap");
  const c = state.categories;
  if (c == null || !Array.isArray(c.rows) || c.rows.length === 0) {
    wrap.innerHTML = '<p class="muted">Category analysis unavailable for the stored window.</p>';
    return;
  }
  const rows = c.rows.map((r) => {
    const tested = r.decision === "tested";
    const decisionHtml = tested
      ? `<span class="chip ${r.significant ? "chip-alert" : "chip-ok"}">
           ${r.significant ? "Flagged" : "No flag"}</span> <span class="muted">adjusted p ${fmtP(r.adjusted_pvalue)}</span>`
      : `<span class="chip chip-warn">No claim: insufficient distinct samples</span>
         <span class="muted">${escapeHtml(r.reason || "")}</span>`;
    return `
    <div class="cat-row">
      <div class="cat-name">${escapeHtml(r.category)}</div>
      <div class="cat-decision">${decisionHtml}</div>
      <div class="cat-counts muted">
        Reference: ${r.unique_reference} of ${r.n_reference} distinct
        &middot; Current: ${r.unique_current} of ${r.n_current} distinct
        &middot; minimum ${c.min_samples_per_category} distinct per side to test
      </div>
    </div>`;
  }).join("");
  wrap.innerHTML = `
    <div class="cat-grid">${rows}</div>
    <p class="panel-note">Category windows span ${c.category_window_cycles} cycles per
      side. Counts are evidence, not scores: a skipped category received no test, so no
      category p value exists anywhere on this page.</p>`;
}

/* ---------- recent activity and alerts ---------- */

function renderActivity() {
  const el = $("activity-list");
  const rows = (state.results || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No completed cycles stored yet.</p>';
    return;
  }
  el.innerHTML = rows.map((r) => {
    const ref = Number(r.reference_size), cur = Number(r.current_size);
    const insufficient = !r.reference_size || !r.current_size;
    let label, cls;
    if (insufficient) { label = "Insufficient data"; cls = "chip-warn"; }
    else if (Number(r.overall_drift_detected) === 1) {
      label = "Drift flagged"; cls = "chip-alert";
    } else if (Number(r.overall_quality_degradation) === 1) {
      label = "Quality flagged"; cls = "chip-alert";
    } else { label = "No drift"; cls = "chip-ok"; }
    let fired = [];
    const raw = r.bh_significant_tests;
    try { fired = typeof raw === "string" ? JSON.parse(raw) : (raw || []); } catch (e) { fired = []; }
    const extra = fired.length ? ` (${fired.join(", ")})` : "";
    return `
      <div class="act-row">
        <span class="chip ${cls}">${label}${extra}</span>
        <span class="muted">cycle ${r.id} &middot; ${fmtTime(r.timestamp)} &middot; ${cur} vs ${ref} responses</span>
      </div>`;
  }).join("");
}

function renderAlerts() {
  const el = $("alerts-list");
  const rows = (state.alerts || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No alerts have been raised. With the persistence rule active, a single flagged cycle stays silent.</p>';
    return;
  }
  el.innerHTML = rows.map((a) => {
    const type = a.alert_type === "drift" ? "Distribution change" : "Quality regression";
    const parts = [];
    parts.push(a.alert_type === "drift"
      ? "The distributional family flagged for consecutive cycles"
      : "The quality family flagged for consecutive cycles");
    if (a.mmd_pvalue != null) parts.push(`semantic p ${fmtP(a.mmd_pvalue)}`);
    if (a.ks_pvalue != null) parts.push(`length p ${fmtP(a.ks_pvalue)}`);
    if (a.proportion_z_pvalue != null) parts.push(`validity p ${fmtP(a.proportion_z_pvalue)}`);
    return `
      <details class="alert">
        <summary>
          <span class="chip chip-alert">${escapeHtml(type)} &middot; ${escapeHtml(a.severity || "")}</span>
          <span class="muted">${fmtTime(a.timestamp)}</span>
        </summary>
        <div class="alert-body">
          <p>${escapeHtml(a.message)}</p>
          <p class="muted">${parts.join(" &middot; ")}</p>
          <p class="muted">What an operator should look at: the per prompt comparison above,
            which detector fired, and the effect sizes for the flagged cycle.</p>
        </div>
      </details>`;
  }).join("");
}

/* ---------- load all ---------- */

async function loadAll() {
  const [health, config, stats, latest, results, alerts,
    comparison, categories] = await Promise.all([
      fetchJson("/health"),
      fetchJson("/config").catch(() => null),
      fetchJson("/stats").catch(() => null),
      fetchJson("/drift/latest").catch(() => null),
      fetchJson("/drift/results?limit=12").catch(() => []),
      fetchJson("/alerts?limit=10").catch(() => []),
      fetchJson("/drift/comparison").catch(() => null),
      fetchJson("/drift/categories").catch(() => null),
    ]);

  state.health = health;
  state.config = config;
  state.stats = stats;
  state.latest = latest;
  state.results = results || [];
  state.alerts = alerts || [];
  state.comparison = comparison;
  state.categories = categories;
  state.awake = true;
  renderAll();
  return true;
}

function renderAll() {
  renderHeadlineHealth();
  renderKpis();
  renderConfig();
  renderDetectors();
  renderEffects();
  renderComparison();
  renderCategories();
  renderActivity();
  renderAlerts();
  /* The paired diagnostic reads /drift/latest, available on every load,
     so it renders whether or not the evaluation tab has been opened. */
  renderPairedDiagnostic();
}

/* ---------- evaluation tab ---------- */

/* Hand-rolled SVG charts: no chart library, consistent with the design
   system, and the numbers plotted are exactly the committed CSV rows.
   Axes are linear; only two small data families are ever plotted. */

function svgScatter(points, opts) {
  if (!points.length) return '<p class="muted">Chart data unavailable.</p>';
  const w = 520, h = 260, m = { top: 16, right: 24, bottom: 40, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const xMax = (Math.max(...points.map((p) => p.x)) * 1.15) || 1;
  const yMax = (Math.max(...points.map((p) => p.y)) * 1.15) || 1;
  const sx = (v) => m.left + (v / xMax) * iw;
  const sy = (v) => m.top + ih - (v / yMax) * ih;
  const parts = [];
  for (let gy = 0; gy <= 4; gy++) {
    const y = m.top + (ih * gy) / 4;
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${m.left + iw}" y2="${y}" stroke="#E4E4DD" stroke-width="1"/>`);
  }
  for (let gx = 0; gx <= 4; gx++) {
    const x = m.left + (iw * gx) / 4;
    parts.push(`<line x1="${x}" y1="${m.top}" x2="${x}" y2="${m.top + ih}" stroke="#E4E4DD" stroke-width="1"/>`);
  }
  parts.push(`<line x1="${m.left}" y1="${m.top + ih}" x2="${m.left + iw}" y2="${m.top + ih}" stroke="#D2D2C9" stroke-width="1"/>`);
  parts.push(`<line x1="${m.left}" y1="${m.top}" x2="${m.left}" y2="${m.top + ih}" stroke="#D2D2C9" stroke-width="1"/>`);
  for (const p of points) {
    const color = p.shipped ? "#B34A1E" : "#40453F";
    const r = p.shipped ? 6 : 4.5;
    parts.push(`<circle cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${r}" fill="${color}"/>`);
    const anchor = sx(p.x) > m.left + iw - 80 ? "end" : "start";
    parts.push(`<text x="${(sx(p.x) + (anchor === "end" ? -9 : 9)).toFixed(1)}" y="${(sy(p.y) + 4).toFixed(1)}" font-size="11" font-family="ui-monospace, monospace" fill="${color}" text-anchor="${anchor}">${p.label}${p.shipped ? " (shipped)" : ""}</text>`);
  }
  parts.push(`<text x="${m.left + iw / 2}" y="${h - 8}" font-size="11" fill="#6C726B" text-anchor="middle">${opts.xLabel}</text>`);
  parts.push(`<text x="14" y="${m.top + ih / 2}" font-size="11" fill="#6C726B" text-anchor="middle" transform="rotate(-90 14 ${m.top + ih / 2})">${opts.yLabel}</text>`);
  parts.push(`<text x="${m.left}" y="${h - 8}" font-size="10" fill="#6C726B" text-anchor="start">0</text>`);
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img">${parts.join("")}</svg>`;
}

function svgLines(xs, series, opts) {
  if (!xs.length) return '<p class="muted">Chart data unavailable.</p>';
  const w = 520, h = 260, m = { top: 16, right: 24, bottom: 40, left: 52 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const xMax = Math.max(...xs) || 1;
  const yMax = opts.yMax || 1;
  const sx = (v) => m.left + (v / xMax) * iw;
  const sy = (v) => m.top + ih - (v / yMax) * ih;
  const parts = [];
  for (let gy = 0; gy <= 4; gy++) {
    const y = m.top + (ih * gy) / 4;
    const label = ((1 - gy / 4) * yMax).toFixed(yMax === 1 && gy % 2 === 0 ? 0 : 1);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${m.left + iw}" y2="${y}" stroke="#E4E4DD" stroke-width="1"/>`);
    parts.push(`<text x="${m.left - 6}" y="${y + 3}" font-size="10" fill="#6C726B" text-anchor="end">${label}</text>`);
  }
  for (const x of xs) {
    parts.push(`<line x1="${sx(x)}" y1="${m.top}" x2="${sx(x)}" y2="${m.top + ih}" stroke="#E4E4DD" stroke-width="1"/>`);
    parts.push(`<text x="${sx(x)}" y="${h - 18}" font-size="10" fill="#6C726B" text-anchor="middle">${x.toFixed(1)}</text>`);
  }
  parts.push(`<line x1="${m.left}" y1="${m.top + ih}" x2="${m.left + iw}" y2="${m.top + ih}" stroke="#D2D2C9" stroke-width="1"/>`);
  parts.push(`<line x1="${m.left}" y1="${m.top}" x2="${m.left}" y2="${m.top + ih}" stroke="#D2D2C9" stroke-width="1"/>`);
  for (const s of series) {
    const segs = [];
    let open = false;
    xs.forEach((x, i) => {
      const v = s.values[i];
      if (v == null) { open = false; return; }
      segs.push(`${open ? "L" : "M"}${sx(x).toFixed(1)} ${sy(v).toFixed(1)}`);
      open = true;
    });
    if (segs.length) {
      parts.push(`<path d="${segs.join(" ")}" fill="none" stroke="${s.color}" stroke-width="2"/>`);
    }
    xs.forEach((x, i) => {
      const v = s.values[i];
      if (v == null) return;
      parts.push(`<circle cx="${sx(x).toFixed(1)}" cy="${sy(v).toFixed(1)}" r="2.6" fill="${s.color}"/>`);
    });
    const labelIdx = s.values.reduce((best, v, i) => (v != null ? i : best), 0);
    const ly = sy(s.values[labelIdx] == null ? 0 : s.values[labelIdx]);
    parts.push(`<text x="${(sx(xs[labelIdx]) + 8).toFixed(1)}" y="${(ly + 4).toFixed(1)}" font-size="11" font-family="ui-monospace, monospace" fill="${s.color}">${s.name}</text>`);
  }
  parts.push(`<text x="${m.left + iw / 2}" y="${h - 4}" font-size="11" fill="#6C726B" text-anchor="middle">${opts.xLabel}</text>`);
  parts.push(`<text x="14" y="${m.top + ih / 2}" font-size="11" fill="#6C726B" text-anchor="middle" transform="rotate(-90 14 ${m.top + ih / 2})">${opts.yLabel}</text>`);
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img">${parts.join("")}</svg>`;
}

function renderPairedDiagnostic() {
  /* Sourced from /drift/latest: the same row the Console detector cards
     read. The swap pattern count is derived the same way
     paired_cluster_mmd_test derives it: exhaustive enumeration when there
     are at most 12 pairs (2^12 = 4096 patterns), seeded sampling above. */
  const l = state.latest || {};
  const set = (id, value) => {
    const el = $(id);
    if (value == null) {
      el.textContent = "not recorded";
      el.className = "effect-value effect-missing";
    } else {
      el.textContent = value;
      el.className = "effect-value";
    }
  };
  set("paired-stat",
    l.paired_cluster_mmd_statistic == null ? null :
      Number(l.paired_cluster_mmd_statistic).toFixed(3));
  set("paired-p",
    l.paired_cluster_mmd_pvalue == null ? null : fmtP(l.paired_cluster_mmd_pvalue));
  set("paired-pairs", l.paired_cluster_prompt_pairs);
  const pairs = l.paired_cluster_prompt_pairs;
  if (pairs != null && pairs > 0) {
    const patterns = pairs <= 12 ? Math.pow(2, pairs) : null;
    set("paired-patterns",
      patterns == null ? "sampled (above the 4096 cap)" : `${patterns} (exhaustive)`);
  } else if (pairs === 0) {
    set("paired-patterns", "0: unmatched window");
  } else {
    set("paired-patterns", null);
  }
}

async function loadEvaluation() {
  try {
    state.evaluation = await fetchJson("/evaluation");
  } catch (err) {
    $("eval-headline").innerHTML =
      '<p class="muted">Evaluation results unavailable in this deployment.</p>';
    return;
  }
  renderEvaluation();
}

function renderEvaluation() {
  const ev = state.evaluation;
  const h = ev.headline || {};
  const sigma = h.evaluated_shift_sigma;
  $("ev-delay").textContent =
    h.mean_detection_delay_days == null ? "n/a" : `${Number(h.mean_detection_delay_days).toFixed(2)} d`;
  $("ev-delay-sub").textContent =
    `under the evaluated simulation, shift magnitude ${sigma} sigma, with the shipped alert policy (${h.default_rule})`;
  $("ev-fa").textContent =
    h.false_alarms_per_100_days == null ? "n/a" : Number(h.false_alarms_per_100_days).toFixed(2);
  $("ev-detect").textContent =
    h.detection_rate_pct == null ? "n/a" : `${Number(h.detection_rate_pct).toFixed(0)}%`;
  $("ev-detect-sub").textContent =
    `detection rate for that particular simulated shift (${sigma} sigma) not a detection rate over all possible shifts`;
  $("ev-overhead").textContent =
    h.overhead_p95_seconds == null ? "n/a" : `${Math.round(h.overhead_p95_seconds * 1000)} ms`;

  const table = (headers, rows) => `
    <table class="plain-table"><thead><tr>
      ${headers.map((x) => `<th>${x}</th>`).join("")}
    </tr></thead><tbody>
      ${rows.map((r) => `<tr>${r.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;

  /* Delay vs false-alarm plane: one point per alert policy. Speed of
     detection improves toward the left, quiet-day safety improves toward
     the bottom, so the best policies sit in the lower left corner. */
  const tradeoffPts = ev.tradeoff
    .filter((r) => r.mean_detection_delay_days != null
      && r.false_alarms_per_100_days != null)
    .map((r) => ({
      x: Number(r.mean_detection_delay_days),
      y: Number(r.false_alarms_per_100_days),
      label: r.rule,
      shipped: r.rule === h.default_rule,
    }));
  $("eval-tradeoff-chart").innerHTML = svgScatter(tradeoffPts, {
    xLabel: "Mean detection delay (days)",
    yLabel: "False alarms per 100 quiet days",
  });
  $("eval-tradeoff").innerHTML = table(
    ["Alert policy", "Mean detection delay (days)", "Detection rate", "False alarms per 100 quiet days"],
    ev.tradeoff.map((r) => [
      `${escapeHtml(r.rule)}${r.rule === h.default_rule ? " <span class='chip chip-cat'>shipped</span>" : ""}`,
      Number(r.mean_detection_delay_days).toFixed(2),
      Number(r.detection_rate_pct).toFixed(1) + "%",
      Number(r.false_alarms_per_100_days).toFixed(2),
    ]));

  const mmdLookup = Object.fromEntries(ev.power_curves.mmd_n10.map((r) => [r.delta, r.power]));
  const ksLookup = Object.fromEntries(ev.power_curves.ks_n10.map((r) => [r.delta, r.power]));
  const deltas = Array.from(new Set([...Object.keys(mmdLookup), ...Object.keys(ksLookup)]
    .map(Number).sort((a, b) => a - b)));
  $("eval-power-chart").innerHTML = svgLines(deltas, [
    {
      name: "MMD (n=10)",
      color: "#B34A1E",
      values: deltas.map((d) => (mmdLookup[d] == null ? null : Number(mmdLookup[d]))),
    },
    {
      name: "KS (n=10)",
      color: "#6C726B",
      values: deltas.map((d) => (ksLookup[d] == null ? null : Number(ksLookup[d]))),
    },
  ], {
    xLabel: "Shift magnitude (sigma)",
    yLabel: "Power",
    yMax: 1,
  });
  $("eval-power").innerHTML = table(
    ["Shift magnitude (sigma)", "MMD power (n=10)", "KS power (n=10)"],
    deltas.map((d) => [
      d.toFixed(1),
      mmdLookup[d] == null ? "n/a" : (100 * Number(mmdLookup[d])).toFixed(0) + "%",
      ksLookup[d] == null ? "n/a" : (100 * Number(ksLookup[d])).toFixed(0) + "%",
    ]));

  const condNames = {
    null: "Quiet (nothing changed)", drift_only: "Distribution shift only",
    invalid_only: "Quality regression only", both: "Shift and regression",
  };
  const signals = {
    pooled_bh: "Pooled BH family", dist_bh: "Distributional BH family",
    mmd: "MMD", ks: "KS", validity: "Validity test",
  };
  const relRows = ev.reliability_conditions
    .filter((r) => ["drift_only", "invalid_only", "both"].includes(r.condition)
      && ["pooled_bh", "dist_bh", "validity"].includes(r.signal));
  const grouped = relRows.reduce((acc, r) => {
    acc[r.condition] = acc[r.condition] || {}; acc[r.condition][r.signal] = r;
    return acc;
  }, {});
  $("eval-reliability").innerHTML = table(
    ["Simulated condition", "Pooled BH family", "Distribution-only family", "Validity family"],
    Object.keys(grouped).map((cond) => [
      condNames[cond] || cond,
      pct(grouped[cond].pooled_bh),
      pct(grouped[cond].dist_bh),
      pct(grouped[cond].validity),
    ]));

  $("eval-power").insertAdjacentHTML("afterend",
    `<p class="panel-note">Detection rate is a family of per-condition results; the
      headline numbers above belong to one evaluated shift, and none of them apply to this
      demo service's runtime behaviour.</p>`);
}

function pct(row) {
  if (!row || !row.detection_rate) return "n/a";
  return (100 * Number(row.detection_rate)).toFixed(0) + "%";
}

/* ---------- wake loop, shared by every load ---------- */

let wakePromise = null;

function fmtWait(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function ensureAwake() {
  if (state.awake) return Promise.resolve(true);
  if (wakePromise) return wakePromise;
  wakePromise = (async () => {
    const startedAt = Date.now();
    state.wakeStartedAt = startedAt;
    let explained = false;
    for (;;) {
      setPill("pill-wake", `connecting · ${fmtWait(Date.now() - startedAt)}`);
      try {
        if (await loadAll()) {
          banner("Connected. All panels reflect the stored monitoring history.");
          return true;
        }
      } catch (err) {
        state.awake = false; /* sleeping instance; keep placeholders, never fake numbers */
      }
      const waited = Date.now() - startedAt;
      if (!explained && waited > 75000) {
        explained = true;
        banner("Still connecting: the free hosting plan pauses this service between visits, and deploys restart it too. The page keeps trying; the first connection is the slow one.");
      }
      await new Promise((r) => setTimeout(r, WAKE_WAIT_MS));
    }
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
    const up = await Promise.race([
      ensureAwake(),
      new Promise((r) => setTimeout(() => r(false), 180000)),
    ]);
    if (!up) {
      note.textContent = "Still connecting in the background. Keep this tab open; the panels fill in when the service responds.";
      return;
    }

    let started;
    try {
      started = await fetchJson("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
    } catch (err) {
      if (err.message && err.message.includes("429")) {
        note.textContent = "A check was just started. It takes one to two minutes; the panels update when it finishes.";
        await loadAll();
        return;
      }
      throw err;
    }
    const runId = started.run_id;
    const cfg = state.config || {};
    note.textContent = `Running: ${cfg.probes_per_cycle || 10} responses are collected and scored, which takes one to two minutes.`;

    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) continue;

      if (st.status === "completed") {
        const r = st.result || {};
        if (r.tests_run) {
          const latest = await fetchJson("/drift/latest").catch(() => null);
          if (latest) { state.latest = latest; renderKpis(); renderDetectors(); renderEffects(); }
          const drifted = latest ? Number(latest.drift_detected) === 1 : false;
          const fired = Array.isArray((r.results || {}).bh_significant_tests)
            ? (r.results || {}).bh_significant_tests : [];
          note.textContent = drifted
            ? "Drift flagged in this cycle. Consult the detector cards and the alert panel for the evidence."
            : "Cycle complete. No statistically significant difference from the reference window.";
          if (drifted && fired.length) {
            banner("Drift flagged: " + fired.join(", ") + " fired after multiple testing correction. Note: an alert reaches history only when the verdict repeats for consecutive cycles.");
          }
        } else {
          note.textContent = "Cycle complete: " + (cfg.current_window || 10) + " responses stored. Tests begin once " + (cfg.tests_start_at_responses || 60) + " responses are stored. Run more checks to fill the reference window.";
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
initTabs();
ensureAwake();
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) {
    loadAll().catch(() => {
      state.awake = false;
      ensureAwake();
    });
  }
}, 60000);
setInterval(() => {
  if (!state.awake && state.wakeStartedAt) {
    setPill("pill-wake", `connecting · ${fmtWait(Date.now() - state.wakeStartedAt)}`);
  }
}, 1000);
