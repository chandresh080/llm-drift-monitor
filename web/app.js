/* Drift Monitor dashboard.
   Vanilla JS, no dependencies; every displayed value comes from the REST
   API on the same origin. Nothing is computed client side except formatting.

   Layout contract: the initial viewport holds the verdict, three summary
   metrics, three detector rows and the effect sizes. Everything else is
   collapsed or lives on the Evaluation tab, so a visitor reads the state
   of the monitor within seconds.

   Loading contract: every data endpoint is fetched and rendered
   independently, so each panel appears at the earliest moment its own
   data is available. Status is derived only from /health/live (fast);
   the slow readiness check never blocks or rewrites the pill. */

const state = {
  live: null,         // /health/live payload (fast liveness)
  config: null,       // /config payload
  stats: null,        // {probe_runs, test_results, alerts}
  latest: null,       // /drift/latest payload
  results: [],        // recent stored cycles
  alerts: [],         // recent persisted alerts
  comparison: null,   // /drift/comparison
  categories: null,   // /drift/categories
  evaluation: null,   // /evaluation
  providerComparison: null,  // recorded Groq vs Gemini comparison (static asset)
  failed: new Set(),  // data endpoints that did not answer
  awake: false,       // at least one data endpoint has answered
  wakeStartedAt: null,
};

const WAKE_WAIT_MS = 5000;
const REQUEST_TIMEOUT_MS = 15000;
const NUM_TESTS = 3;      // detector family: KS, availability, MMD
const DAILY_CYCLE_BUDGET = 15;  // display fallback; /config run_policy wins

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return "—";
  return t.toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function fmtP(p) {
  if (p == null) return "—";
  return Number(p) < 0.001 ? "< 0.001" : Number(p).toFixed(3);
}

function fmtWait(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
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
   attempt. Errors carry the response status and any Retry-After the
   server sent, so callers can explain rate limits instead of guessing. */
async function fetchJson(url, opts = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const resp = await fetch(url, { ...opts, signal: controller.signal });
    if (!resp.ok) {
      const err = new Error(`HTTP ${resp.status}`);
      err.status = resp.status;
      err.retryAfter = resp.headers.get("Retry-After");
      try {
        const body = await resp.json();
        if (body && body.detail) err.detail = body.detail;
      } catch (e) { /* non json error body: status is explanation enough */ }
      throw err;
    }
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
  pill.innerHTML = `<span class="dot"></span>${escapeHtml(text)}`;
}

/* The pill answers exactly one question: is the service up right now.
   It is derived from /health/live alone, so a slow readiness check can
   never produce a confusing intermediate wording. */
function renderHealthPill() {
  if (state.live == null) {
    setPill(null, "connecting");
    return;
  }
  const ok = state.live.status === "ok";
  setPill(ok ? "pill-ok" : "pill-bad", ok ? "Online" : "Offline");
}

/* ---------- model label in the header ---------- */

function renderModelLabel() {
  const c = state.config;
  const el = $("model-label");
  if (!c || !c.target_model) {
    el.textContent = state.failed.has("config") ? "" : "";
    return;
  }
  el.textContent = c.provider_available === false
    ? `${c.target_model} · unavailable`
    : c.target_model;
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

/* ---------- split signal families ---------- */

/* The families are separate by design: the distribution headline follows
   the distributional detectors only. The availability test and the
   absolute invalid-share guardrail are quality signals and are reported
   in their own strip, never merged into the verdict. Legacy history
   recorded before the split fields existed falls back to the per test
   flags. */
function latestSignals(l) {
  const dist = l.distribution_shift_detected != null
    ? Number(l.distribution_shift_detected)
    : (Number(l.ks_significant) === 1 || Number(l.mmd_significant) === 1 ? 1 : 0);
  const qualityChange = l.quality_change_detected != null
    ? Number(l.quality_change_detected)
    : Number(l.proportion_z_significant) === 1;
  const guardrail = Number(l.quality_degradation) === 1;
  return { dist, qualityChange: Boolean(qualityChange), guardrail };
}

/* ---------- verdict ---------- */

function renderVerdict() {
  const latest = state.latest;
  const title = $("kpi-drift");
  const sub = $("verdict-sub");
  const meta = $("verdict-meta");
  const strip = $("quality-strip");
  const evaluated = latest && (latest.drift_detected != null
    || latest.distribution_shift_detected != null);

  if (!evaluated) {
    title.textContent = "Not evaluated yet";
    title.className = "verdict-title";
    sub.textContent = "Statistical evaluation starts once a full reference "
      + "window and one current cycle are stored.";
    meta.textContent = "No completed cycle yet.";
    strip.hidden = true;
    return;
  }

  const sig = latestSignals(latest);
  if (sig.dist === 1) {
    title.textContent = "Shift detected";
    title.className = "verdict-title verdict-alert";
    sub.textContent = "A distributional detector flagged after "
      + "Benjamini-Hochberg correction in the latest evaluation.";
  } else {
    title.textContent = "No shift detected";
    title.className = "verdict-title verdict-ok";
    sub.textContent = "No statistically significant distributional change "
      + "was detected in the latest evaluation.";
  }

  meta.textContent =
    `Reference: ${Number(latest.reference_size)} responses · `
    + `Current: ${Number(latest.current_size)} responses · `
    + `Last checked: ${fmtTime(latest.timestamp)}`;

  /* Quality signals stay separate: chips only when one actually fired. */
  const parts = [];
  if (sig.qualityChange) {
    parts.push('<span class="chip chip-alert">Response availability changed</span>');
  }
  if (sig.guardrail) {
    parts.push('<span class="chip chip-alert">Invalid-response guardrail tripped</span>');
  }
  if (parts.length) {
    parts.push('<span class="chip-note">quality signal, not a distribution verdict</span>');
    strip.hidden = false;
    strip.innerHTML = parts.join("");
  } else {
    strip.hidden = true;
    strip.innerHTML = "";
  }
}

/* ---------- summary metrics ---------- */

function renderMetrics() {
  const latest = state.latest;

  const runsEl = $("kpi-runs");
  if (state.stats && state.stats.probe_runs != null) {
    runsEl.textContent = Number(state.stats.probe_runs).toLocaleString();
    runsEl.className = "metric-value";
  } else {
    runsEl.textContent = "—";
    runsEl.className = "metric-value effect-missing";
  }

  const fired = (latest && Array.isArray(latest.flagged_tests))
    ? latest.flagged_tests.length : null;
  const flaggedEl = $("kpi-flagged");
  if (latest && fired != null) {
    flaggedEl.textContent = `${fired} / ${NUM_TESTS}`;
    flaggedEl.className = "metric-value" + (fired > 0 ? " kpi-alert" : "");
  } else {
    flaggedEl.textContent = "—";
    flaggedEl.className = "metric-value effect-missing";
  }

  const lastEl = $("kpi-lastcheck");
  if (latest && latest.timestamp) {
    lastEl.textContent = fmtTime(latest.timestamp);
    lastEl.className = "metric-value";
  } else {
    lastEl.textContent = "—";
    lastEl.className = "metric-value effect-missing";
  }
}

/* ---------- detector table ---------- */

/* One row per detector: name, method, adjusted p-value, decision. The
   full string is built from the data in one pass; absent values decide
   as "Not evaluated", never as "No flag". */
function renderDetectors() {
  const l = state.latest;
  const rows = [
    {
      name: "Response length",
      method: "KS",
      p: l ? l.ks_adjusted_pvalue : null,
      sig: l ? l.ks_significant : null,
    },
    {
      name: "Response availability",
      method: l && l.proportion_z_test_method === "fisher" ? "Fisher exact"
        : "Fisher / z",
      p: l ? l.proportion_z_adjusted_pvalue : null,
      sig: l ? l.proportion_z_significant : null,
    },
    {
      name: "Semantic shift",
      method: "Paired MMD",
      p: l ? l.mmd_adjusted_pvalue : null,
      sig: l ? l.mmd_significant : null,
    },
  ];
  $("detector-rows").innerHTML = rows.map((r) => {
    let decision;
    if (r.p == null && r.sig == null) {
      decision = '<span class="chip chip-warn">Not evaluated</span>';
    } else if (Number(r.sig) === 1) {
      decision = '<span class="chip chip-alert">Flagged</span>';
    } else {
      decision = '<span class="chip chip-ok">No flag</span>';
    }
    return `
      <tr data-testid="detector-row">
        <td class="detector-name">${escapeHtml(r.name)}</td>
        <td class="detector-method">${escapeHtml(r.method)}</td>
        <td class="num">${r.p == null ? "—" : fmtP(r.p)}</td>
        <td>${decision}</td>
      </tr>`;
  }).join("");
}

/* ---------- effect sizes ---------- */

function renderEffects() {
  const l = state.latest || {};
  const set = (id, value) => {
    const el = $(id);
    if (value == null) {
      el.textContent = "—";
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
  /* Sign comes from the change itself, never from the reference level. */
  const delta = (l.refusal_rate_current == null || l.refusal_rate_reference == null)
    ? null : (l.refusal_rate_current - l.refusal_rate_reference);
  set("eff-refusal",
    delta == null ? null : `${delta > 0 ? "+" : ""}${(100 * delta).toFixed(1)} pp`);
}

/* ---------- run note and configuration ---------- */

/* The operational note under the button uses the real configured values:
   how many prompts a run collects and the interval a visitor must wait
   for. It never hardcodes numbers another deployment would contradict. */
function renderRunNote() {
  const c = state.config;
  if (!c || $("run-note").dataset.active) return;
  const rp = c.run_policy || {};
  const intervalMin = rp.per_visitor_interval_seconds != null
    ? Math.round(rp.per_visitor_interval_seconds / 60) : null;
  const budget = rp.daily_cycle_budget != null ? rp.daily_cycle_budget : DAILY_CYCLE_BUDGET;
  const bits = [`${c.probes_per_cycle || c.prompt_panel_probes || "?"} prompts per run`];
  if (intervalMin != null) bits.push(`one check per visitor every ${intervalMin} min`);
  bits.push(`up to ${budget} checks per day`);
  $("run-note").textContent = bits.join(" · ");
}

function renderConfig() {
  const c = state.config;
  if (!c) {
    $("cfg-model").textContent = state.failed.has("config")
      ? "unavailable" : "…";
    return;
  }
  $("cfg-model").textContent = c.provider_available === false
    ? `${c.target_model} (unavailable to run new cycles)`
    : `${c.target_model} (${c.adapter_type})`;
  $("cfg-embedding").textContent = c.embedding_model;
  const cyclesForWindow = c.probes_per_cycle
    ? Math.round(c.reference_window / c.probes_per_cycle) : null;
  $("cfg-ref").textContent = cyclesForWindow
    ? `${c.reference_window} responses (${cyclesForWindow} cycles)`
    : `${c.reference_window} responses`;
  $("cfg-cur").textContent = `${c.current_window} responses (1 cycle)`;
  $("cfg-panel").textContent =
    `v${c.prompt_panel_version} · ${c.prompt_panel_probes} prompts · `
    + `${c.probes_per_cycle} per cycle`;
  $("cfg-alpha").textContent =
    `${Number(c.alpha).toFixed(2)} (Benjamini-Hochberg)`;
  $("cfg-persist").textContent =
    `${c.min_consecutive_cycles} consecutive flagged cycles`;
  const ts = state.latest && state.latest.timestamp;
  $("cfg-last-run").textContent = ts
    ? `${fmtTime(ts)} · ${Number(state.latest.current_size)} vs `
      + `${Number(state.latest.reference_size)} responses`
    : "none yet";

  const l = state.latest || {};
  $("cfg-latency").textContent =
    l.provider_latency_mean_seconds == null
      ? "not recorded"
      : `${Number(l.provider_latency_mean_seconds).toFixed(1)} s mean · `
        + `${Number(l.provider_latency_p50_seconds).toFixed(1)} s p50 · `
        + `${Number(l.provider_latency_p95_seconds).toFixed(1)} s p95`;
  $("cfg-tokens").textContent =
    l.probe_tokens_total == null
      ? "not recorded"
      : `${Number(l.probe_tokens_total).toLocaleString()} tokens per cycle`;

  const m = c.baseline_manifest;
  $("cfg-manifest").textContent = m
    ? `${m.provider} ${m.model_id} · panel v${m.panel_version} · `
      + `temperature ${m.temperature} · ${fmtTime(m.collected_at)}`
    : "not recorded";

  const rp = c.run_policy || {};
  const used = rp.daily_cycles_used_today != null
    ? Number(rp.daily_cycles_used_today) : null;
  $("cfg-runpolicy").textContent =
    `1 per visitor / ${Math.round((rp.per_visitor_interval_seconds
      || 600) / 60)} min · ${rp.daily_cycle_budget != null
      ? rp.daily_cycle_budget : DAILY_CYCLE_BUDGET} per day`
    + (used != null ? ` (${used} used today)` : "");
  renderRunNote();
  renderModelLabel();
}

/* ---------- response samples ---------- */

function sampleStatus(data) {
  if (!data || data.text == null) return '<span class="muted">no stored answer</span>';
  const chips = [`<span class="sample-meta">${data.length} chars</span>`];
  if (data.is_valid != null) {
    chips.push(`<span class="chip ${data.is_valid ? "chip-ok" : "chip-alert"}">`
      + `${data.is_valid ? "non-empty" : "empty"}</span>`);
  }
  if (data.is_refusal === 1) {
    chips.push('<span class="chip chip-alert">refusal</span>');
  }
  return chips.join("");
}

function renderComparison() {
  const wrap = $("comparison-wrap");
  const c = state.comparison;
  if (c == null) {
    wrap.innerHTML = `<p class="empty-state">${
      state.failed.has("comparison")
        ? "Comparison endpoint unavailable." : "Comparison unavailable."}</p>`;
    return;
  }
  if (!c.ready) {
    wrap.innerHTML = `
      <p class="empty-state">Evaluation has not started yet: ${c.reference_stored} of
      ${c.reference_required} reference and ${c.current_stored} of ${c.current_required}
      current responses stored.</p>`;
    return;
  }
  const rows = c.rows.map((r) => {
    const label = r.label || r.prompt_text;
    const redacted = Boolean(r.prompt_redacted);
    const chips = [
      `<span class="chip chip-cat">${escapeHtml(r.category)}</span>`,
      redacted ? '<span class="chip chip-warn">withheld</span>' : "",
      sampleStatus(r.reference),
      sampleStatus(r.current),
    ].filter(Boolean).join("");
    const body = redacted
      ? `<p class="sample-withheld">Safety boundary probe: the exact request and the
           model's answers are not published. Lengths and validity flags above remain
           visible.</p>`
      : `
        <p class="prompt-line">${escapeHtml(r.prompt_text)}</p>
        <div class="sample-col">
          <h4>Reference</h4>
          <code class="resp-text">${escapeHtml(
            r.reference && r.reference.text != null ? r.reference.text : "—")}</code>
        </div>
        <div class="sample-col">
          <h4>Current</h4>
          <code class="resp-text">${escapeHtml(
            r.current && r.current.text != null ? r.current.text : "—")}</code>
        </div>`;
    return `
      <details class="sample" data-redacted="${redacted ? "1" : "0"}">
        <summary>
          <span class="sample-label">${escapeHtml(label)}</span>
          ${chips}
        </summary>
        <div class="sample-body">${body}</div>
      </details>`;
  }).join("");
  wrap.innerHTML = `<div class="sample-list">${rows}</div>`;
}

/* ---------- category analysis ---------- */

function renderCategories() {
  const wrap = $("categories-wrap");
  const c = state.categories;
  if (c == null || !Array.isArray(c.rows) || c.rows.length === 0) {
    wrap.innerHTML = `<p class="empty-state">Not reported for the stored window: `
      + `category tests need ${c && c.min_samples_per_category
        ? c.min_samples_per_category : 8} distinct samples per category per side.</p>`;
    return;
  }
  const rows = c.rows.map((r) => {
    const tested = r.decision === "tested";
    const decision = tested
      ? `<span class="chip ${r.significant ? "chip-alert" : "chip-ok"}">
           ${r.significant ? "Flagged" : "No flag"}</span>
         <span class="muted">p ${fmtP(r.adjusted_pvalue)}</span>`
      : `<span class="chip chip-warn">Not tested</span>
         <span class="muted">${escapeHtml(r.reason || "insufficient distinct samples")}</span>`;
    return `
      <div class="cat-row">
        <span class="cat-name">${escapeHtml(r.category)}</span>
        ${decision}
        <span class="cat-counts muted">${r.unique_reference}/${r.n_reference} ·
          ${r.unique_current}/${r.n_current} distinct</span>
      </div>`;
  }).join("");
  wrap.innerHTML = `<div>${rows}</div>`;
}

/* ---------- activity: cycles and alerts ---------- */

function renderActivity() {
  const el = $("activity-list");
  if (state.failed.has("results")) {
    el.innerHTML = '<p class="empty-state">Recent cycles unavailable.</p>';
    return;
  }
  const rows = (state.results || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="empty-state">No completed cycles stored yet.</p>';
    return;
  }
  const body = rows.map((r) => {
    const insufficient = !r.reference_size || !r.current_size;
    const distShifted = Number(r.ks_significant) === 1
      || Number(r.mmd_significant) === 1;
    const fired = (() => {
      const raw = r.bh_significant_tests;
      try {
        return typeof raw === "string" ? JSON.parse(raw) : (raw || []);
      } catch (e) { return []; }
    })();
    let verdict;
    if (insufficient) verdict = '<span class="chip chip-warn">Insufficient data</span>';
    else if (distShifted) {
      verdict = `<span class="chip chip-alert">Shift flagged${
        fired.length ? ` · ${escapeHtml(fired.join(", "))}` : ""}</span>`;
    } else verdict = '<span class="chip chip-ok">No shift</span>';
    return `
      <tr>
        <td>${fmtTime(r.timestamp)}</td>
        <td>#${r.id}</td>
        <td>${verdict}</td>
      </tr>`;
  }).join("");
  el.innerHTML = `
    <p class="act-subhead">Recent cycles</p>
    <table class="plain-table">
      <thead><tr><th>Time</th><th>Cycle</th><th>Result</th></tr></thead>
      <tbody>${body}</tbody>
    </table>`;
}

function renderAlerts() {
  const el = $("alerts-list");
  if (state.failed.has("alerts")) {
    el.innerHTML = '<p class="empty-state">Alerts unavailable.</p>';
    return;
  }
  const rows = (state.alerts || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="act-subhead">Alerts</p><p class="empty-state">No alerts.</p>';
    return;
  }
  const body = rows.map((a) => {
    const type = a.alert_type === "drift" ? "Distribution shift" : "Quality signal";
    return `
      <div class="alert-row">
        <span class="chip chip-alert">${escapeHtml(type)}</span>
        <span class="muted">${fmtTime(a.timestamp)}</span>
        <span class="alert-msg">${escapeHtml(a.message)}</span>
      </div>`;
  }).join("");
  el.innerHTML = `<p class="act-subhead">Alerts</p>${body}`;
}

/* ---------- data loading ---------- */

let wakePromise = null;

/* Each data endpoint is its own task: fetch, store, and render exactly the
   panels that depend on it. A failed endpoint records itself in
   state.failed so its panel renders an explicit unavailable state. */
const DATA_LOADERS = [
  {
    key: "config", url: "/config",
    render: () => { renderConfig(); renderRunNote(); },
  },
  {
    key: "stats", url: "/stats",
    render: renderMetrics,
  },
  {
    key: "latest", url: "/drift/latest",
    render: () => {
      renderVerdict();
      renderMetrics();
      renderConfig();
      renderDetectors();
      renderEffects();
      renderPairedDiagnostic();
    },
  },
  { key: "results", url: "/drift/results?limit=12", render: renderActivity },
  { key: "alerts", url: "/alerts?limit=10", render: renderAlerts },
  { key: "comparison", url: "/drift/comparison", render: renderComparison },
  { key: "categories", url: "/drift/categories", render: renderCategories },
];

async function loadOne(def) {
  try {
    state[def.key] = await fetchJson(def.url);
    state.failed.delete(def.key);
  } catch (err) {
    state[def.key] = null;
    state.failed.add(def.key);
  }
  try {
    def.render();
  } catch (err) { /* a renderer must never take the page down */ }
  if (state[def.key] != null && !state.awake) {
    state.awake = true;
  }
}

function loadAll() {
  return Promise.allSettled(DATA_LOADERS.map((def) => loadOne(def)));
}

/* Fast liveness probe: the single source of the status pill. A failure
   means the instance is unreachable, which is an Offline state — distinct
   from the pre-answer "connecting" state. */
async function loadLive() {
  try {
    state.live = await fetchJson("/health/live");
  } catch (err) {
    state.live = { status: "unreachable" };
  }
  renderHealthPill();
}

function ensureAwake() {
  if (state.awake) return Promise.resolve(true);
  if (wakePromise) return wakePromise;
  if (!state.wakeStartedAt) state.wakeStartedAt = Date.now();
  const startedAt = state.wakeStartedAt;
  wakePromise = (async () => {
    let explained = false;
    for (;;) {
      const tasks = DATA_LOADERS.map((def) => loadOne(def));
      await Promise.race([
        Promise.any(tasks).catch(() => false),
        new Promise((r) => setTimeout(r, 8000)),
      ]);
      if (state.awake) return true;
      const waited = Date.now() - startedAt;
      if (!explained && waited > 75000) {
        explained = true;
        banner("Still connecting: the free hosting plan pauses this service "
          + "between visits and after deploys. The page keeps trying; nothing "
          + "samples the model until you press Run check.");
      }
      await new Promise((r) => setTimeout(r, WAKE_WAIT_MS));
    }
  })();
  wakePromise.finally(() => { wakePromise = null; });
  return wakePromise;
}

/* ---------- paired permutation diagnostic ---------- */

function renderPairedDiagnostic() {
  /* From /drift/latest. The swap pattern count mirrors
     paired_cluster_mmd_test: exhaustive enumeration when there are at
     most 12 pairs (4096 patterns), seeded sampling above. */
  const l = state.latest || {};
  const set = (id, value) => {
    const el = $(id);
    if (value == null) {
      el.textContent = "—";
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
      patterns == null ? "sampled" : `${patterns} (exhaustive)`);
  } else if (pairs === 0) {
    set("paired-patterns", "0 (unmatched window)");
  } else {
    set("paired-patterns", null);
  }
}

/* ---------- evaluation tab ---------- */

async function loadEvaluation() {
  loadProviderComparison();
  try {
    state.evaluation = await fetchJson("/evaluation");
  } catch (err) {
    $("evaluation-body").innerHTML =
      '<p class="empty-state">Evaluation results unavailable.</p>';
    return;
  }
  renderEvaluation();
}

/* The recorded Groq versus Gemini comparison is a committed static
   artifact built by scripts/export_provider_comparison.py. */
async function loadProviderComparison() {
  try {
    state.providerComparison =
      await fetchJson("/static/provider_comparison.json");
    state.failed.delete("providerComparison");
  } catch (err) {
    state.providerComparison = null;
    state.failed.add("providerComparison");
  }
  try {
    renderProviderComparison();
  } catch (err) { /* a renderer must never take the page down */ }
}

function renderProviderComparison() {
  const pc = state.providerComparison;
  const box = $("prov-cmp");
  const verdict = $("prov-cmp-verdict");
  if (!box) return;
  if (!pc) {
    box.innerHTML = '<p class="empty-state">Recorded comparison unavailable.</p>';
    if (verdict) verdict.textContent = "";
    return;
  }
  const agg = (rows) => {
    const p50 = rows.map((r) => r.latency_p50_s).filter((v) => v != null);
    const p95 = rows.map((r) => r.latency_p95_s).filter((v) => v != null);
    const tokens = rows.map((r) => r.tokens_total).filter((v) => v != null);
    const ok = rows.reduce((s, r) => s + (r.ok_runs || 0), 0);
    const failed = rows.reduce((s, r) => s + (r.failed_runs || 0), 0);
    const range = (xs, digits) => xs.length
      ? `${Math.min(...xs).toFixed(digits)} to ${Math.max(...xs).toFixed(digits)}`
      : null;
    return { p50: range(p50, 2), p95: range(p95, 2), tokens: range(tokens, 0), ok, failed };
  };
  const ref = agg(pc.reference.cycles);
  const cur = agg(pc.current.cycles);
  const validity = (rows) => {
    const vals = rows.map((r) => r.validity_rate).filter((v) => v != null);
    if (!vals.length) return "not measured";
    const min = Math.min(...vals) * 100, max = Math.max(...vals) * 100;
    return min === max ? `${min.toFixed(0)}%` : `${min.toFixed(0)} to ${max.toFixed(0)}%`;
  };
  const row = (label, stage, model, a, v) => `
    <tr><td>${label}</td><td>${stage}</td><td>${model}</td>
    <td>${a.ok} / ${a.failed}</td><td>${a.p50 || "—"}</td>
    <td>${a.p95 || "—"}</td><td>${a.tokens || "—"}</td><td>${v}</td></tr>`;
  box.innerHTML = `
    <table class="plain-table"><thead><tr>
      <th>Provider</th><th>Stage</th><th>Model</th><th>Stored answers</th>
      <th>Latency p50 (s)</th><th>Latency p95 (s)</th><th>Tokens/cycle</th>
      <th>Validity</th>
    </tr></thead><tbody>
      ${row(pc.reference.provider, "reference, 5 cycles",
        pc.reference.model, ref, validity(pc.reference.cycles))}
      ${row(pc.current.provider, "current, 6 cycles",
        pc.current.model, cur, validity(pc.current.cycles))}
    </tbody></table>`;
  if (verdict) {
    const f1 = (pc.families || {}).single_cycle || {};
    const f2 = (pc.families || {}).extended_final || {};
    const noFlag = !f1.any_significant && !f2.any_significant;
    verdict.innerHTML =
      `Cross-provider, cross-model evidence that the adapters work — not a drift `
      + `result. ${noFlag
        ? "No corrected test flagged in either family, and no alert fired. "
        : "One or more tests flagged; see the committed artifacts. "}`
      + `Paired permutation raw p ${fmtP(f1.paired_mmd_pvalue)} over `
      + `${f1.paired_prompt_pairs} matched prompts. Declared differences: `
      + `${pc.shared.uncontrolled_differences.join("; ")}. Sources: `
      + `${pc.source_artifacts.join(", ")}. ${pc.caveat}`;
  }
}

/* Numeric keys everywhere: the CSV reader returns delta values as strings
   such as "0.0", and looking those up with the number 1 would miss the key
   "1.0" entirely. Rounds to one decimal, the resolution the source uses. */
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
    h.mean_detection_delay_days == null ? "—" : `${Number(h.mean_detection_delay_days).toFixed(2)} d`;
  $("ev-fa").textContent =
    h.false_alarms_per_100_days == null ? "—" : Number(h.false_alarms_per_100_days).toFixed(2);
  $("ev-detect").textContent =
    h.detection_rate_pct == null ? "—" : `${Number(h.detection_rate_pct).toFixed(0)}%`;
  $("ev-overhead").textContent =
    h.overhead_p95_seconds == null ? "—" : `${Math.round(h.overhead_p95_seconds * 1000)} ms`;

  const table = (headers, rows) => `
    <table class="plain-table"><thead><tr>
      ${headers.map((x) => `<th>${x}</th>`).join("")}
    </tr></thead><tbody>
      ${rows.map((r) => `<tr>${r.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;

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
    ["Alert policy", "Delay (days)", "Detection rate", "False alarms / 100 d"],
    ev.tradeoff.map((r) => [
      `${escapeHtml(r.rule)}${r.rule === h.default_rule ? " (shipped)" : ""}`,
      Number(r.mean_detection_delay_days).toFixed(2),
      Number(r.detection_rate_pct).toFixed(1) + "%",
      Number(r.false_alarms_per_100_days).toFixed(2),
    ]));

  const mmd = powerLookup(ev.power_curves.mmd_n10);
  const ks = powerLookup(ev.power_curves.ks_n10);
  const deltas = Array.from(new Set([...mmd.keys(), ...ks.keys()])).sort((a, b) => a - b);
  $("eval-power-chart").innerHTML = svgLines(deltas, [
    { name: "MMD", color: "#4F46E5", values: deltas.map((d) => (mmd.has(d) ? mmd.get(d) : null)) },
    { name: "KS", color: "#71717A", values: deltas.map((d) => (ks.has(d) ? ks.get(d) : null)) },
  ], {
    xLabel: "Shift magnitude (sigma)",
    yLabel: "Detection power",
    yMax: 1,
  });
  const trials = ev.power_curves.trials_per_point;
  $("eval-power").innerHTML = table(
    ["Shift (sigma)", "MMD power (n=10)", "KS power (n=10)"],
    deltas.map((d) => [
      d.toFixed(1),
      mmd.has(d) ? Math.round(100 * mmd.get(d)) + "%" : "n/a",
      ks.has(d) ? Math.round(100 * ks.get(d)) + "%" : "n/a",
    ]));

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

  $("ev-methodology").innerHTML =
    `Results come from controlled simulations with predefined shifts, sample sizes `
    + `and alert policies (sources: <code>experiments/</code>, <code>results/</code>). `
    + `The delay and power numbers were produced by pooled, iid simulation: they `
    + `characterise the persistence policy and each statistic's behaviour, not the `
    + `production paired alert path. Detection rate ${sigma} sigma is estimated from `
    + `a limited number of trials${trials ? ` (${trials} per power point)` : ""}, `
    + `not a guarantee across all shifts. Category-level localization is weak at the `
    + `evaluated sample sizes. The provider comparison below is a real hosted call `
    + `experiment comparing different models — it cannot establish temporal drift `
    + `within one model. Full methodology: <a href="https://github.com/chandresh080/llm-drift-monitor/blob/main/METRICS.md" target="_blank" rel="noopener">METRICS.md</a>.`;
}

function pct(row) {
  if (!row || !row.detection_rate) return "n/a";
  return (100 * Number(row.detection_rate)).toFixed(0) + "%";
}

/* ---------- hand-rolled SVG charts ---------- */

/* No chart library; consistent with the design system; the plotted values
   are exactly the committed data rows. Both charts get numeric tick labels
   on both axes and a legend. */

function chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy) {
  const parts = [];
  for (const [v, label] of yTicks) {
    const y = sy(v);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${m.left + (w - m.left - m.right)}" y2="${y}" stroke="#F0F0F2" stroke-width="1"/>`);
    parts.push(`<text x="${m.left - 6}" y="${y + 3}" font-size="10" fill="#71717A" text-anchor="end">${label}</text>`);
  }
  for (const [v, label] of xTicks) {
    const x = sx(v);
    parts.push(`<line x1="${x}" y1="${m.top}" x2="${x}" y2="${h - m.bottom}" stroke="#F0F0F2" stroke-width="1"/>`);
    parts.push(`<text x="${x}" y="${h - m.bottom + 12}" font-size="10" fill="#71717A" text-anchor="middle">${label}</text>`);
  }
  parts.push(`<line x1="${m.left}" y1="${h - m.bottom}" x2="${w - m.right}" y2="${h - m.bottom}" stroke="#D4D4D8" stroke-width="1"/>`);
  parts.push(`<line x1="${m.left}" y1="${m.top}" x2="${m.left}" y2="${h - m.bottom}" stroke="#D4D4D8" stroke-width="1"/>`);
  parts.push(`<text x="${m.left + (w - m.left - m.right) / 2}" y="${h - 4}" font-size="11" fill="#71717A" text-anchor="middle">${opts.xLabel}</text>`);
  parts.push(`<text x="13" y="${m.top + (h - m.top - m.bottom) / 2}" font-size="11" fill="#71717A" text-anchor="middle" transform="rotate(-90 13 ${m.top + (h - m.top - m.bottom) / 2})">${opts.yLabel}</text>`);
  return parts;
}

function legendHtml(series) {
  return `<div class="chart-legend">${series.map((s) => `
    <span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${escapeHtml(s.name)}</span>`).join("")}</div>`;
}

function svgScatter(points, opts) {
  if (!points.length) return '<p class="empty-state">Chart data unavailable.</p>';
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
  const parts = chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy);
  for (const p of points) {
    const color = p.shipped ? "#4F46E5" : "#3F3F46";
    parts.push(`<circle cx="${sx(p.x).toFixed(1)}" cy="${sy(p.y).toFixed(1)}" r="${p.shipped ? 6 : 4.5}" fill="${color}"/>`);
  }
  const shipped = points.find((p) => p.shipped) || points[0];
  /* Near the x-axis the label would collide with the ticks: place it above
     the point instead of below. */
  const labelBelow = sy(shipped.y) < h * 0.6;
  const labelY = labelBelow ? sy(shipped.y) + 18 : sy(shipped.y) - 10;
  parts.push(`<text x="${sx(shipped.x).toFixed(1)}" y="${labelY.toFixed(1)}" font-size="11" font-family="ui-monospace, monospace" fill="#4F46E5" text-anchor="middle">${shipped.label}</text>`);
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img">${parts.join("")}</svg>`
    + legendHtml(points.map((p) => ({
        name: p.label + (p.shipped ? " (shipped)" : ""),
        color: p.shipped ? "#4F46E5" : "#3F3F46",
      })));
}

function svgLines(xs, series, opts) {
  if (!xs.length) return '<p class="empty-state">Chart data unavailable.</p>';
  const w = 520, h = 250, m = { top: 14, right: 20, bottom: 42, left: 56 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const xMax = Math.max(...xs, 0.5);
  const yMax = opts.yMax || 1;
  const sx = (v) => m.left + (v / xMax) * iw;
  const sy = (v) => m.top + ih - (v / yMax) * ih;
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((v) => [v, String(Math.round(v * 100)) + "%"]);
  const xTicks = xs.map((v) => [v, v.toFixed(1)]);
  const parts = chartChrome(w, h, m, opts, xTicks, yTicks, sx, sy);
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
  btn.textContent = "Running…";
  $("run-note").dataset.active = "1";
  try {
    /* Bounded wait for connectivity on a cold-started instance. */
    const up = await Promise.race([
      ensureAwake(),
      new Promise((r) => setTimeout(() => r(false), 45000)),
    ]);
    if (!up) {
      note.textContent = "The service has not answered yet (cold start can take "
        + "a few minutes). This page keeps trying in the background.";
      return;
    }

    let started;
    try {
      started = await fetchJson("/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
    } catch (err) {
      if (err.status === 429) {
        let when = "";
        const waitS = err.retryAfter ? parseInt(err.retryAfter, 10) : NaN;
        if (!isNaN(waitS) && waitS < 86400) {
          when = " A run can be started at about "
            + new Date(Date.now() + waitS * 1000).toLocaleTimeString(
              undefined, { hour: "2-digit", minute: "2-digit" }) + ".";
        }
        note.textContent = (err.detail
          ? err.detail
          : "A rate limit was reached (per visitor interval or the daily budget).")
          + when;
        return;
      }
      throw err;
    }
    const runId = started.run_id;
    const cfg = state.config || {};
    note.textContent = `Running: ${cfg.probes_per_cycle || 40} responses are `
      + `collected and evaluated; this can take several minutes.`;

    /* Hosted cycles take minutes; poll patiently. A free tier recycle
       mid check erases the run record: consecutive misses stop the loop
       with an honest message instead of polling a ghost forever. */
    let misses = 0;
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 10000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) {
        misses += 1;
        if (misses >= 3) {
          note.textContent = "The hosted instance recycled while this check ran, "
            + "so its record is gone and nothing was committed. Run it again.";
          await loadAll();
          return;
        }
        continue;
      }
      misses = 0;

      if (st.status === "lost") {
        note.textContent = st.error || "The instance recycled during this check; run again.";
        await loadAll();
        return;
      }

      if (st.status === "completed") {
        const r = st.result || {};
        await loadAll();
        if (r.tests_run) {
          const latest = state.latest || {};
          const sig = latestSignals(latest);
          note.textContent = sig.dist === 1
            ? "Distribution shift flagged in this cycle; an alert fires only if it repeats next cycle."
            : "Cycle complete. No statistically significant distributional change.";
        } else {
          note.textContent = `Cycle complete: ${cfg.current_window || 40} responses `
            + `stored. Evaluation starts at ${cfg.tests_start_at_responses || 160}.`;
        }
        return;
      }
      if (st.status === "failed") {
        note.textContent = "That run failed: " + (st.error || "unknown issue")
          + (String(st.error || "").includes("Provider unavailable")
            ? " New checks stay unavailable until the provider key is configured."
            : " Nothing was committed; wait a moment and try again.");
        await loadAll();
        return;
      }
    }
    note.textContent = "The run is still going in the background; panels update when it finishes.";
  } catch (err) {
    banner("Could not start a check: " + err.message);
    note.textContent = "No new responses were recorded.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Run check";
    runInFlight = false;
    /* The note's dataset.active flag stays set: whatever the last run
       wrote (a rate limit, a verdict, a failure) should persist until the
       visitor starts the next check, not be overwritten by the baseline. */
  }
}

/* ---------- start ---------- */

$("run-btn").addEventListener("click", runCheck);
initTabs();
loadLive();        // pill: Online / Offline as soon as liveness answers
ensureAwake();     // data panels, each rendering individually as they land
/* Periodic refresh only when connected and visible; failed endpoints are
   retried here. If nothing answers anymore, resume the wake loop. */
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) {
    loadAll();
    loadLive();
    if (state.failed.size >= DATA_LOADERS.length) {
      state.awake = false;
      ensureAwake();
    }
  }
}, 60000);
