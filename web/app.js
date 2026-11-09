/* Drift Monitor live console.
   Vanilla JS, no dependencies; all numbers come from the REST API on the
   same origin. Nothing is computed client side except formatting.

   Loading contract: every panel renders from whatever its own endpoint
   returned, as soon as it arrives. No single endpoint, health included,
   can block another panel. Health is status context, not a gate. */

const state = {
  health: null,       // /health payload (readiness; may be slow or absent)
  config: null,       // /config payload
  stats: null,        // {probe_runs, test_results, alerts}
  latest: null,       // /drift/latest payload
  results: [],        // recent stored cycles
  alerts: [],         // recent persisted alerts
  comparison: null,   // /drift/comparison
  categories: null,   // /drift/categories
  evaluation: null,   // /evaluation
  awake: false,       // at least one data endpoint has answered
  wakeStartedAt: null,
};

const WAKE_WAIT_MS = 5000;
const REQUEST_TIMEOUT_MS = 15000;
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
  banner._t = setTimeout(() => { el.hidden = true; }, 12000);
}

/* Real abortable timeout: the fetch itself is cancelled when the timer
   fires, so a stalled request cannot linger and stack up behind the next
   attempt. */
async function fetchJson(url, opts = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { ...opts, signal: controller.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

/* ---------- status pill ---------- */

function setPill(cls, text) {
  const pill = $("health-pill");
  pill.className = "pill";
  if (cls) pill.classList.add(cls);
  pill.setAttribute("data-status", text.split(" ")[0]);
  pill.innerHTML = `<span class="dot"></span>${escapeHtml(text)}`;
}

/* The pill reflects two independent things: whether stored data could be
   read at all, and whether the readiness check reports problems. Data
   availability wins the slot; readiness only downgrades the wording. */
function renderHealthPill() {
  if (!state.awake) {
    setPill("pill-wake",
      `connecting · ${fmtWait(Date.now() - (state.wakeStartedAt || Date.now()))}`);
    return;
  }
  if (state.health == null) {
    setPill("pill-wake", "Data loaded · status check pending");
    return;
  }
  const okAll = state.health.status === "ok";
  setPill(okAll ? "pill-ok" : "pill-bad",
    okAll ? "Service online" : "Service degraded");
  if (!okAll) {
    banner(`The service is reachable but a dependency reported a problem: `
      + `provider ${state.health.provider_status}, database ${state.health.db}. `
      + `Panels show the stored history either way.`);
  }
}

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
  const qualityStrip = $("quality-strip");
  if (!latest || latest.drift_detected == null) {
    driftEl.textContent = "No evaluation yet";
    driftEl.className = "kpi-value";
    driftSub.textContent = "statistical evaluation starts once a full reference window and one current cycle are stored";
    qualityStrip.hidden = true;
  } else {
    const drifted = Number(latest.drift_detected) === 1;
    const degraded = Number(latest.quality_degradation) === 1;
    if (drifted) {
      driftEl.textContent = "Shift flagged";
      driftEl.className = "kpi-value kpi-alert";
    } else {
      driftEl.textContent = "Stable";
      driftEl.className = "kpi-value kpi-ok";
    }
    driftSub.textContent =
      `distribution shift status for the latest cycle (${fmtTime(latest.timestamp)}) vs the reference window`;
    driftSub.title = detectedWithoutQualityCopy(drifted);
    /* Separate quality status. The guardrail is an absolute threshold on
       the current window, not a test against the reference, so it is
       reported as its own line rather than folded into the verdict. */
    qualityStrip.hidden = false;
    if (degraded) {
      qualityStrip.innerHTML =
        `<span class="chip chip-alert">Quality guardrail tripped</span>
         <span class="muted">the invalid response share reached the configured `
        + `30% threshold in the current window. This is an absolute guardrail on `
        + `one window, not a statistical comparison with the reference window.</span>`;
      banner("Quality guardrail tripped: the current window's invalid response rate "
        + "crossed the configured threshold. Check the per prompt comparison panel.");
    } else {
      qualityStrip.innerHTML =
        `<span class="chip chip-ok">Quality guardrail: ok</span>
         <span class="muted">invalid response share under the configured `
        + `threshold in the current window (absolute guardrail, statistical `
        + `comparison lives in the detectors below).</span>`;
    }
  }

  /* Canonical field: flagged_tests (the API also serves the legacy name
     bh_significant_tests with the same array). */
  const fired = (latest && Array.isArray(latest.flagged_tests))
    ? latest.flagged_tests.length : 0;
  const flaggedEl = $("kpi-flagged");
  flaggedEl.textContent = latest ? `${fired} / ${NUM_TESTS}` : "...";
  flaggedEl.className = "kpi-value" + (fired > 0 ? " kpi-alert" : "");

  const runsEl = $("kpi-runs");
  if (state.stats && state.stats.probe_runs != null) {
    runsEl.textContent = Number(state.stats.probe_runs).toLocaleString();
    runsEl.className = "kpi-value";
  } else {
    runsEl.textContent = "unavailable";
    runsEl.className = "kpi-value effect-missing";
  }

  /* Monitoring backend label describes what generated the stored answers:
     the demo adapter here, a local model under other configurations. */
  const backendEl = $("kpi-provider");
  if (state.health && state.health.provider) {
    backendEl.textContent = state.health.provider;
    backendEl.className = "kpi-value";
  } else if (state.config && state.config.target_model) {
    backendEl.textContent = state.config.target_model;
    backendEl.className = "kpi-value";
  } else {
    backendEl.textContent = "unavailable";
    backendEl.className = "kpi-value effect-missing";
  }
}

function detectedWithoutQualityCopy(drifted) {
  return drifted
    ? "At least one detector flagged after Benjamini-Hochberg correction. "
      + "An alert requires the flag to persist across consecutive cycles."
    : "Stable means no statistically significant distributional difference "
      + "was found in the latest cycle. It does not mean the model is good, "
      + "and it says nothing about behaviour outside the probed prompts.";
}

function renderConfig() {
  const c = state.config;
  if (!c) {
    $("cfg-model").closest(".config-details").classList.add("effect-missing");
    return;
  }
  $("cfg-model").textContent = `${c.target_model} (${c.adapter_type})`;
  $("cfg-embedding").textContent = c.embedding_model;
  $("cfg-ref").textContent = `${c.reference_window} responses (5 complete cycles)`;
  $("cfg-cur").textContent = `${c.current_window} responses (1 cycle)`;
  $("cfg-panel").textContent =
    `v${c.prompt_panel_version}, ${c.prompt_panel_probes} prompts, ${c.probes_per_cycle} per cycle`;
  $("cfg-alpha").textContent = Number(c.alpha).toFixed(2)
    + " after Benjamini-Hochberg correction across the test family";
  $("cfg-persist").textContent =
    `${c.min_consecutive_cycles} consecutive flagged cycles before an alert fires `
    + `(practical noise reduction, not a formal sequential error control guarantee)`;
  const ts = state.latest && state.latest.timestamp;
  $("cfg-last-run").textContent = ts
    ? `${fmtTime(ts)} (${Number(state.latest.current_size)} responses vs ${Number(state.latest.reference_size)})`
    : "no cycle recorded yet";
  $("run-note").textContent =
    `Each run records ${c.current_window} responses. Statistical evaluation `
    + `starts once ${c.tests_start_at_responses} responses are stored (one `
    + `reference window plus one current cycle).`;
}

/* ---------- detector cards and effect sizes ---------- */

function detectorCard(prefix, p, significant) {
  const decision = $(prefix + "-decision");
  if (p == null && significant == null) {
    decision.textContent = "Not evaluated";
    decision.className = "chip chip-warn";
  } else {
    decision.textContent = Number(significant) === 1 ? "Flagged" : "No flag";
    decision.className = "chip " + (Number(significant) === 1 ? "chip-alert" : "chip-ok");
  }
  $(prefix + "-p").textContent = p == null ? "not recorded" : fmtP(p);
}

function renderDetectors() {
  const l = state.latest;
  if (!l) {
    ["ks", "prop", "mmd"].forEach((prefix) => {
      $(prefix + "-decision").textContent = "Data unavailable";
      $(prefix + "-decision").className = "chip chip-warn";
      $(prefix + "-p").textContent = "unavailable";
    });
    return;
  }
  detectorCard("det-ks", l.ks_adjusted_pvalue, l.ks_significant);
  detectorCard("det-prop", l.proportion_z_adjusted_pvalue, l.proportion_z_significant);
  detectorCard("det-mmd", l.mmd_adjusted_pvalue, l.mmd_significant);
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
  /* Sign comes from the change itself, never from the reference level:
     a negative delta must show as negative whatever the reference was. */
  const delta = (l.refusal_rate_current == null || l.refusal_rate_reference == null)
    ? null : (l.refusal_rate_current - l.refusal_rate_reference);
  set("eff-refusal",
    delta == null ? null : `${delta > 0 ? "+" : ""}${(100 * delta).toFixed(1)} pp`);
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
    wrap.innerHTML = '<p class="muted">Comparison panel unavailable in this deployment.</p>';
    return;
  }
  if (!c.ready) {
    wrap.innerHTML = `
      <div class="empty-state">
        <strong>Evaluation has not started yet.</strong><br>
        The monitor maintains a reference window of ${c.reference_required} responses
        (the baseline) and a current window of ${c.current_required} (what is compared
        against it). Right now the service holds ${c.reference_stored} of
        ${c.reference_required} reference responses and ${c.current_stored} of
        ${c.current_required} current responses. Once ${c.tests_start_at_responses}
        responses are stored, each cycle runs all three tests and this panel shows the
        answers side by side. This is expected behaviour until then, not a fault.
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
    <p class="panel-note">Click a row to read the complete responses. Reference shows the
      newest answer to each prompt still inside the baseline window; current shows the
      newest answer from the latest cycle.</p>`;

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

/* ---------- category level analysis ---------- */

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
    <p class="panel-note">Category tests examine whether pooled distributional changes
      concentrate in particular prompt groups; windows span ${c.category_window_cycles}
      cycles per side. Results are reported only when the distinct sample count supports
      inference; otherwise the available evidence and the skip reason are shown. These
      rows are evidence, not category scores.</p>`;
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
      label = "Shift flagged"; cls = "chip-alert";
    } else if (Number(r.overall_quality_degradation) === 1) {
      label = "Quality guardrail tripped"; cls = "chip-alert";
    } else { label = "No shift"; cls = "chip-ok"; }
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
    el.innerHTML = '<p class="muted">No alerts raised yet. An alert requires the same '
      + 'verdict on consecutive cycles; a single flagged cycle is recorded but stays silent.</p>';
    return;
  }
  el.innerHTML = rows.map((a) => {
    const type = a.alert_type === "drift" ? "Distribution change" : "Quality regression";
    const parts = [];
    parts.push(a.alert_type === "drift"
      ? "The distributional family flagged on consecutive cycles"
      : "The quality family flagged on consecutive cycles");
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
          <p class="muted">Operator starting points: the per prompt comparison above,
            which detectors fired, and the behavioural effect sizes of the flagged cycle.</p>
        </div>
      </details>`;
  }).join("");
}

/* ---------- data loading ---------- */

let wakePromise = null;

function fmtWait(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

async function loadAll() {
  /* Every request has its own error path; /health is the slow readiness
     check and must never hold up the stored monitoring snapshot, which is
     what a first time visitor is here to see. */
  const [health, config, stats, latest, results, alerts,
    comparison, categories] = await Promise.allSettled([
      fetchJson("/health"),
      fetchJson("/config"),
      fetchJson("/stats"),
      fetchJson("/drift/latest"),
      fetchJson("/drift/results?limit=12"),
      fetchJson("/alerts?limit=10"),
      fetchJson("/drift/comparison"),
      fetchJson("/drift/categories"),
    ]);
  const val = (r, fallback) => (r.status === "fulfilled" ? r.value : fallback);

  state.health = val(health, null);
  state.config = val(config, null);
  state.stats = val(stats, null);
  state.latest = val(latest, null);
  state.results = val(results, []) || [];
  state.alerts = val(alerts, []) || [];
  state.comparison = val(comparison, null);
  state.categories = val(categories, null);

  /* Success signal: any of the data endpoints answered. Panels that failed
     render their own explicit unavailable state. */
  const anyData = state.latest != null || state.stats != null
    || state.results.length > 0 || state.comparison != null;
  if (anyData) state.awake = true;
  renderAll();
  return anyData;
}

/* Snapshot-reload map: after one loadAll, re-run only the renderers whose
   inputs may have changed; renderHealthPill is separate so a slow readiness
   check updating later only touches the pill. */
function renderAll() {
  renderHealthPill();
  renderKpis();
  renderConfig();
  renderDetectors();
  renderEffects();
  renderComparison();
  renderCategories();
  renderActivity();
  renderAlerts();
  renderPairedDiagnostic();
}

function ensureAwake() {
  if (state.awake) return Promise.resolve(true);
  if (wakePromise) return wakePromise;
  wakeStarted();
  wakePromise = (async () => {
    const startedAt = state.wakeStartedAt;
    let explained = false;
    for (;;) {
      try {
        if (await loadAll()) {
          banner("Connected. Panels show the stored monitoring history; the status "
            + "pill confirms dependency readiness separately.");
          return true;
        }
      } catch (err) {
        state.awake = false;
      }
      const waited = Date.now() - startedAt;
      if (!explained && waited > 75000) {
        explained = true;
        banner("Still connecting: the free hosting plan pauses this service between "
          + "visits and after deploys. The page keeps trying; nothing here samples "
          + "the target model until you press Run a check.");
      }
      await new Promise((r) => setTimeout(r, WAKE_WAIT_MS));
    }
  })();
  wakePromise.finally(() => { wakePromise = null; });
  return wakePromise;
}

function wakeStarted() {
  if (!state.wakeStartedAt) state.wakeStartedAt = Date.now();
}

/* ---------- evaluation tab ---------- */

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
    $("evaluation-body").innerHTML =
      '<p class="muted">Evaluation results unavailable in this deployment.</p>';
    return;
  }
  renderEvaluation();
}

/* Numeric keys everywhere: the CSV reader returns delta values as strings
   such as "0.0", and looking those up with the number 1 would miss the key
   "1.0" entirely and render n/a. Rounds to one decimal, the resolution the
   source data itself uses. */
function powerLookup(rows) {
  const map = new Map();
  for (const r of rows) {
    map.set(Number(Number(r.delta).toFixed(1)), Number(r.power));
  }
  return map;
}

function renderEvaluation() {
  const ev = state.evaluation;
  const h = ev.headline || {};
  const sigma = h.evaluated_shift_sigma;
  $("ev-delay").textContent =
    h.mean_detection_delay_days == null ? "n/a" : `${Number(h.mean_detection_delay_days).toFixed(2)} d`;
  $("ev-delay-sub").textContent =
    `under the evaluated simulated shift (${sigma} sigma), with the shipped alert policy (${h.default_rule})`;
  $("ev-fa").textContent =
    h.false_alarms_per_100_days == null ? "n/a" : Number(h.false_alarms_per_100_days).toFixed(2);
  $("ev-detect").textContent =
    h.detection_rate_pct == null ? "n/a" : `${Number(h.detection_rate_pct).toFixed(0)}%`;
  $("ev-detect-sub").textContent =
    `for that particular evaluated shift (${sigma} sigma), estimated from a limited `
    + `number of simulation trials, not a guarantee across all shifts`;

  const table = (headers, rows) => `
    <table class="plain-table"><thead><tr>
      ${headers.map((x) => `<th>${x}</th>`).join("")}
    </tr></thead><tbody>
      ${rows.map((r) => `<tr>${r.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;

  /* Delay vs false-alarm plane: one point per alert policy. Faster
     detection is left, fewer false alarms is down, so the better
     policies sit toward the lower left. */
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
    yUnit: "count",
  });
  $("eval-tradeoff").innerHTML = table(
    ["Alert policy", "Mean detection delay (days)", "Detection rate", "False alarms per 100 quiet days"],
    ev.tradeoff.map((r) => [
      `${escapeHtml(r.rule)}${r.rule === h.default_rule ? " <span class='chip chip-cat'>shipped</span>" : ""}`,
      Number(r.mean_detection_delay_days).toFixed(2),
      Number(r.detection_rate_pct).toFixed(1) + "%",
      Number(r.false_alarms_per_100_days).toFixed(2),
    ]));

  const mmd = powerLookup(ev.power_curves.mmd_n10);
  const ks = powerLookup(ev.power_curves.ks_n10);
  const deltas = Array.from(new Set([...mmd.keys(), ...ks.keys()])).sort((a, b) => a - b);
  $("eval-power-chart").innerHTML = svgLines(deltas, [
    { name: "MMD", color: "#B34A1E", values: deltas.map((d) => (mmd.has(d) ? mmd.get(d) : null)) },
    { name: "KS", color: "#6C726B", values: deltas.map((d) => (ks.has(d) ? ks.get(d) : null)) },
  ], {
    xLabel: "Shift magnitude (sigma)",
    yLabel: "Detection power",
    yMax: 1,
  });
  const trials = ev.power_curves.trials_per_point;
  $("eval-power").innerHTML = table(
    ["Shift magnitude (sigma)", "MMD power (n=10)", "KS power (n=10)"],
    deltas.map((d) => [
      d.toFixed(1),
      mmd.has(d) ? Math.round(100 * mmd.get(d)) + "%" : "n/a",
      ks.has(d) ? Math.round(100 * ks.get(d)) + "%" : "n/a",
    ]));
  if (trials) {
    $("eval-power").insertAdjacentHTML("afterend",
      `<p class="panel-note">Each point is estimated from ${trials} simulation trials. `
      + `Even a real but small shift can remain undetected at a current window of ten `
      + `responses; the low end of both curves shows exactly that limitation.</p>`);
  }

  const condNames = {
    null: "Quiet (nothing changed)", drift_only: "Distribution shift only",
    invalid_only: "Quality regression only", both: "Shift and regression",
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

  $("ev-overhead").textContent =
    h.overhead_p95_seconds == null ? "n/a" : `${Math.round(h.overhead_p95_seconds * 1000)} ms`;
  const overheadNote = h.overhead_samples
    ? `over ${h.overhead_samples} recorded benchmark cycles, `
    : `over the recorded benchmark cycles, `;
  $("ev-overhead-sub").textContent = overheadNote
    + `recomputed from the raw cycle timings, excluding provider inference. `
    + `A percentile over this many samples indicates an order of magnitude, `
    + `not a latency guarantee.`;
}

function pct(row) {
  if (!row || !row.detection_rate) return "n/a";
  return (100 * Number(row.detection_rate)).toFixed(0) + "%";
}

/* ---------- hand-rolled SVG charts ----------

   No chart library; consistent with the design system; the plotted values
   are exactly the committed CSV rows. Both charts get numeric tick labels
   on both axes and a legend, so nothing is named only next to a data
   point where labels can collide. */

function chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy, xMax, yMax) {
  const parts = [];
  for (const [v, label] of yTicks) {
    const y = sy(v);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${m.left + (w - m.left - m.right)}" y2="${y}" stroke="#E4E4DD" stroke-width="1"/>`);
    parts.push(`<text x="${m.left - 6}" y="${y + 3}" font-size="10" fill="#6C726B" text-anchor="end">${label}</text>`);
  }
  for (const [v, label] of xTicks) {
    const x = sx(v);
    parts.push(`<line x1="${x}" y1="${m.top}" x2="${x}" y2="${h - m.bottom}" stroke="#E4E4DD" stroke-width="1"/>`);
    parts.push(`<text x="${x}" y="${h - m.bottom + 12}" font-size="10" fill="#6C726B" text-anchor="middle">${label}</text>`);
  }
  parts.push(`<line x1="${m.left}" y1="${h - m.bottom}" x2="${w - m.right}" y2="${h - m.bottom}" stroke="#D2D2C9" stroke-width="1"/>`);
  parts.push(`<line x1="${m.left}" y1="${m.top}" x2="${m.left}" y2="${h - m.bottom}" stroke="#D2D2C9" stroke-width="1"/>`);
  parts.push(`<text x="${m.left + (w - m.left - m.right) / 2}" y="${h - 4}" font-size="11" fill="#6C726B" text-anchor="middle">${opts.xLabel}</text>`);
  parts.push(`<text x="13" y="${m.top + (h - m.top - m.bottom) / 2}" font-size="11" fill="#6C726B" text-anchor="middle" transform="rotate(-90 13 ${m.top + (h - m.top - m.bottom) / 2})">${opts.yLabel}</text>`);
  return parts;
}

function legendHtml(series) {
  return `<div class="chart-legend">${series.map((s) => `
    <span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${escapeHtml(s.name)}</span>`).join("")}</div>`;
}

function svgScatter(points, opts) {
  if (!points.length) return '<p class="muted">Chart data unavailable.</p>';
  const w = 520, h = 250, m = { top: 14, right: 20, bottom: 42, left: 56 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const xMax = Math.max(1, Math.max(...points.map((p) => p.x)) * 1.12);
  const yMax = Math.max(1, Math.max(...points.map((p) => p.y)) * 1.12);
  const sx = (v) => m.left + (v / xMax) * iw;
  const sy = (v) => m.top + ih - (v / yMax) * ih;
  const yTicks = [0, 1, 2, 3, 4].map((g) => {
    const v = (g / 4) * yMax;
    return [v, Number.isInteger(v) ? String(v) : v.toFixed(1)];
  });
  const xTicks = [0, 0.5, 1, 1.5, 2].filter((v) => v <= xMax + 1e-9)
    .map((v) => [v, v === 0 ? "0" : v.toFixed(1)]);
  const parts = chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy, xMax, yMax);
  for (const p of points) {
    const color = p.shipped ? "#B34A1E" : "#40453F";
    parts.push(`<circle cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${p.shipped ? 6 : 4.5}" fill="${color}"/>`);
  }
  /* Point names live in the legend, plus a small label on the shipped
     point only, which is the one the reader is meant to find. */
  const shipped = points.find((p) => p.shipped) || points[0];
  parts.push(`<text x="${sx(shipped.x).toFixed(1)}" y="${(sy(shipped.y) + 18).toFixed(1)}" font-size="11" font-family="ui-monospace, monospace" fill="#B34A1E" text-anchor="middle">${shipped.label}</text>`);
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img">${parts.join("")}</svg>`
    + legendHtml(points.map((p) => ({
        name: p.label + (p.shipped ? " (shipped)" : ""),
        color: p.shipped ? "#B34A1E" : "#40453F",
      })));
}

function svgLines(xs, series, opts) {
  if (!xs.length) return '<p class="muted">Chart data unavailable.</p>';
  const w = 520, h = 250, m = { top: 14, right: 20, bottom: 42, left: 56 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const xMax = Math.max(...xs, 0.5);
  const yMax = opts.yMax || 1;
  const sx = (v) => m.left + (v / xMax) * iw;
  const sy = (v) => m.top + ih - (v / yMax) * ih;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((v) => [v, String(Math.round(v * 100)) + "%"]);
  const xTicks = xs.map((v) => [v, v.toFixed(1)]);
  const parts = chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy, xMax, yMax);
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
  }
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img">${parts.join("")}</svg>`
    + legendHtml(series);
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
    /* Bounded wait for connectivity: if the service is still waking, say
       so and let the loop below poll rather than silently hanging. */
    const up = await Promise.race([
      ensureAwake(),
      new Promise((r) => setTimeout(() => r(false), 45000)),
    ]);
    if (!up) {
      note.textContent = "The service has not answered yet (free tier cold start can "
        + "take a few minutes). This page keeps trying in the background; the message "
        + "here updates as soon as a check can start.";
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
        note.textContent = "A check was started a moment ago. It takes one to two "
          + "minutes; panels update when it finishes.";
        await loadAll();
        return;
      }
      throw err;
    }
    const runId = started.run_id;
    const cfg = state.config || {};
    note.textContent = `Running: ${cfg.probes_per_cycle || 10} responses are collected `
      + `and evaluated, which takes one to two minutes.`;

    for (let i = 0; i < 120; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) continue;

      if (st.status === "completed") {
        const r = st.result || {};
        if (r.tests_run) {
          await loadAll();
          const latest = state.latest || {};
          const drifted = Number(latest.drift_detected) === 1;
          note.textContent = drifted
            ? "Shift flagged in this cycle. The detector cards and alert panel show "
              + "the evidence; an alert fires only if this repeats next cycle."
            : "Cycle complete. No statistically significant distributional difference "
              + "from the reference window.";
          const fired = Array.isArray(latest.flagged_tests) ? latest.flagged_tests : [];
          if (drifted && fired.length) {
            banner("Shift flagged: " + fired.join(", ") + " after Benjamini-Hochberg "
              + "correction. Alerts fire only when the verdict repeats on consecutive "
              + "cycles; this cycle is recorded either way.");
          }
        } else {
          note.textContent = "Cycle complete: " + (cfg.current_window || 10)
            + " responses stored. Evaluation starts once "
            + (cfg.tests_start_at_responses || 60) + " responses are stored.";
        }
        await loadAll();
        return;
      }
      if (st.status === "failed") {
        note.textContent = "That run failed. Nothing was committed for it; wait a "
          + "moment and try again.";
        banner("The last run did not finish: " + (st.error || "unknown issue"));
        return;
      }
    }
    note.textContent = "The run is still going in the background; panels update when "
      + "it finishes.";
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
/* The pill keeps counting while connecting, without touching panels. */
setInterval(() => {
  if (!state.awake && state.wakeStartedAt) {
    setPill("pill-wake", `connecting · ${fmtWait(Date.now() - state.wakeStartedAt)}`);
  }
}, 1000);
/* Periodic refresh only when connected and visible; on failure, resume the
   wake loop (which retries with its own messaging). */
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) {
    loadAll().catch(() => {
      state.awake = false;
      ensureAwake();
    });
  }
}, 60000);
