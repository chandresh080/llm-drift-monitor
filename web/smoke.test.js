/* Dashboard smoke test, no dependencies, runs in plain Node.
   Loads web/app.js together with a minimal DOM stub and API fixtures, and
   asserts the integration contracts that Python tests cannot see.
   This is a JavaScript contract test, not a browser test: it verifies what
   the code does with the API responses, not rendering, timing or layout.

   Contract list:
   - each data endpoint renders the panel it feeds, independent of the
     other endpoints (no endpoint can block another)
   - API reachability is its own state: online, degraded, unknown, offline
   - provider availability is a separate fact on the model label, never a
     second competing service badge
   - readiness refresh retries unresolved states but reuses decisive ones
     only for the backend provider-ping TTL
   - a finished monitoring run refreshes readiness, not just data panels, so
     an earlier unchecked/failed result cannot survive a successful cycle
   - recent activity is a real table: five newest cycles, newest first
   - detector history needs at least three stored cycles; fewer is a compact
     message, never an empty chart or fabricated data
   - history breaks the line for missing adjusted p-values, marks the latest
     cycle, and draws the alpha threshold
   - the split signal families render separately: a response availability
     flag can never flip the distribution shift headline
   - redacted safety boundary prompts show their public label and a
     withheld marker, never raw wording
   - response samples preview five changed-first rows and reveal the rest
     only through Show all; row text compares actual stored strings
   - missing test values render as an explicit state ("Not evaluated",
     "n/a"), never as "No flag" or zero
   - product CSV string keys ("0.0") resolve against numeric lookups
   - the refusal rate change takes its sign from the delta itself
   - rate limit rejections surface the server's Retry-After message
   - a read-only storage fallback disables new checks and explains why
   - Monitor and Evaluation stay separate views

   Used by CI; run locally with: node web/smoke.test.js */

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

/* ---------- minimal DOM ---------- */

const elements = new Map();

function makeElement(id) {
  const el = {
    id,
    textContent: "",
    innerHTML: "",
    className: "",
    hidden: false,
    title: "",
    disabled: false,
    dataset: {},
    listeners: {},
    classList: {
      add(...cls) { cls.forEach((c) => el.classList._ensure(c)); },
      remove(...cls) { cls.forEach((c) => delete el._classes[c]); },
      toggle(c, on) { if (on) el.classList.add(c); else delete el._classes[c]; },
      contains(c) { return Boolean(el._classes[c]); },
      _ensure(c) { el._classes[c] = true; },
      _classes: {},
    },
    setAttribute(name, value) { el.attrs[name] = value; },
    removeAttribute(name) { delete el.attrs[name]; },
    addEventListener(name, fn) {
      (el.listeners[name] = el.listeners[name] || []).push(fn);
    },
    insertAdjacentHTML(_pos, html) { el.innerHTML += html; },
    querySelector() { return makeElement(id + ":child"); },
    querySelectorAll() { return []; },
    scrollIntoView() {},
    closest() { return makeElement("closest:" + id); },
    attrs: {},
    _classes: {},
  };
  return el;
}

function getById(id) {
  if (!elements.has(id)) elements.set(id, makeElement(id));
  return elements.get(id);
}

const documentStub = {
  getElementById: getById,
  visibilityState: "visible",
};

/* setInterval is stubbed before app.js loads: the script installs periodic
   loops and a test must not keep the event loop alive. */
const timers = [];
global.setInterval = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
global.clearInterval = () => {};

global.document = documentStub;
global.window = global;
global.AbortController = AbortController;

/* app.js declared bindings (const state ...) live in the global lexical
   scope; run small snippets in the same context to reach them. */
function vmEval(code) {
  vm.runInThisContext(code, { filename: "smoke-inline" });
}

/* ---------- API fixtures ---------- */

const latestFull = {
  drift_detected: 1,
  distribution_shift_detected: 1,
  quality_change_detected: 0,
  flagged_tests: ["mmd"],           /* canonical field the API returns */
  bh_significant_tests: ["mmd"],
  quality_degradation: 1,
  timestamp: "2026-11-08T12:00:00",
  reference_size: 120, current_size: 40,
  ks_statistic: 0.3, ks_pvalue: 0.4, ks_adjusted_pvalue: 1.0, ks_significant: 0,
  proportion_z_statistic: null, proportion_z_pvalue: 1.0,
  proportion_z_adjusted_pvalue: 1.0, proportion_z_significant: 0,
  mmd_statistic: 0.2, mmd_pvalue: 0.01, mmd_adjusted_pvalue: 0.03, mmd_significant: 1,
  mmd_permutation_unit: "prompt_cluster", paired_cluster_method: "sampled",
  length_shift_pct: 19.5, centroid_distance: 0.153,
  refusal_rate_reference: 0.0, refusal_rate_current: -0.05,
  unique_reference: 12, unique_current: 10,
  paired_cluster_mmd_statistic: -0.027, paired_cluster_mmd_pvalue: 1.0,
  paired_cluster_prompt_pairs: 10,
  provider_latency_mean_seconds: 4.2,
  provider_latency_p50_seconds: 4.0,
  provider_latency_p95_seconds: 5.1,
  probe_tokens_total: 960,
};

const csvRows = (rows) => rows.map(([delta, power]) =>
  ({ test: "mmd", delta: String(delta), n: "10", power: String(power), n_sims: "50", alpha: "0.05" }));

/* Six stored cycles, deliberately served out of order and with one missing
   adjusted p-value: activity shows the five newest newest-first, and history
   must break the line instead of inventing p = 0. */
const historyRows = [
  { id: 1, timestamp: "2026-11-10T18:22:00", reference_size: 120, current_size: 40,
    overall_drift_detected: 0, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.82, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.64, proportion_z_significant: 0,
    mmd_adjusted_pvalue: null, mmd_significant: null },
  { id: 4, timestamp: "2026-11-18T09:15:00", reference_size: 120, current_size: null,
    overall_drift_detected: 0, overall_quality_degradation: null,
    ks_adjusted_pvalue: 0.91, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.88, proportion_z_significant: 0,
    mmd_adjusted_pvalue: 0.77, mmd_significant: 0 },
  { id: 2, timestamp: "2026-11-16T12:31:00", reference_size: 120, current_size: 40,
    overall_drift_detected: 0, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.72, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.58, proportion_z_significant: 0,
    mmd_adjusted_pvalue: 0.41, mmd_significant: 0 },
  { id: 3, timestamp: "2026-11-17T08:05:00", reference_size: 120, current_size: 40,
    overall_drift_detected: 1, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.048, ks_significant: 1,
    proportion_z_adjusted_pvalue: 0.031, proportion_z_significant: 1,
    mmd_adjusted_pvalue: 0.022, mmd_significant: 1 },
  { id: 6, timestamp: "2026-11-18T20:25:00", reference_size: 120, current_size: 40,
    overall_drift_detected: 0, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.97, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.96, proportion_z_significant: 0,
    mmd_adjusted_pvalue: 0.88, mmd_significant: 0 },
  { id: 5, timestamp: "2026-11-18T14:40:00", reference_size: 120, current_size: 40,
    overall_drift_detected: 0, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.95, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.93, proportion_z_significant: 0,
    mmd_adjusted_pvalue: 0.81, mmd_significant: 0 },
];

/* /cycles is the activity source: one row per monitoring cycle, with the
   detector result attached by exact cycle id. Cycle 4 is deliberately
   interrupted so the activity table has to say so. */
const cycleRows = historyRows.map((r) => ({
  cycle_id: `cycle-${r.id}`,
  cycle_number: r.id,
  started_at: r.timestamp,
  ended_at: r.timestamp,
  timestamp: r.timestamp,
  probe_count: r.id === 4 ? 23 : 40,
  success_count: r.id === 4 ? 23 : 40,
  failed_count: r.id === 4 ? 0 : 0,
  expected_probes: 40,
  status: r.id === 4 ? "interrupted" : "evaluated",
  result: r,
}));

/* Only two completed cycles: enough for activity and for a connected
   two-point history, not enough for a trend. */
const twoCycles = cycleRows.filter((r) => r.cycle_number === 1 || r.cycle_number === 2);

const storedAlert = {
  id: 9, timestamp: "2026-11-17T08:05:00", alert_type: "drift",
  message: "Semantic shift persisted for two consecutive cycles.",
};

/* Eight prompts: two genuinely changed, one redacted safety probe, one row
   whose text is identical but whose stored lengths disagree. Preview must
   be five, changed first, and the identical text must stay unchanged. */
const samplesReady = {
  ready: true, reference_stored: 120, reference_required: 120,
  current_stored: 40, current_required: 40, tests_start_at: 160,
  rows: [
    { prompt_id: "p_alpha", category: "general", label: "Alpha unchanged",
      prompt_text: "Alpha prompt", prompt_redacted: false,
      reference: { text: "same answer", length: 11, is_valid: 1 },
      current: { text: "same answer", length: 11, is_valid: 1 } },
    { prompt_id: "p_bravo", category: "code", label: "Bravo changed",
      prompt_text: "Bravo prompt", prompt_redacted: false,
      reference: { text: "print(1)", length: 8, is_valid: 1 },
      current: { text: "print(2)", length: 8, is_valid: 1 } },
    { prompt_id: "p_charlie", category: "general", label: "Charlie unchanged",
      prompt_text: "Charlie prompt", prompt_redacted: false,
      reference: { text: "steady", length: 6, is_valid: 1 },
      current: { text: "steady", length: 6, is_valid: 1 } },
    { prompt_id: "p_delta", category: "code", label: "Delta changed",
      prompt_text: "Delta prompt", prompt_redacted: false,
      reference: { text: "x = 1", length: 5, is_valid: 1 },
      current: { text: "x = 10", length: 6, is_valid: 1 } },
    { prompt_id: "p_safety", category: "safety_boundary",
      label: "Disallowed synthesis request, details withheld",
      prompt_text: "(withheld: safety boundary prompt)", prompt_redacted: true,
      reference: { text: "(withheld: safety boundary prompt)", length: 90, is_valid: 1 },
      current: { text: "(withheld: safety boundary prompt)", length: 90, is_valid: 1 } },
    { prompt_id: "p_echo", category: "general", label: "Echo unchanged",
      prompt_text: "Echo prompt", prompt_redacted: false,
      reference: { text: "echo", length: 4, is_valid: null },
      current: { text: "echo", length: 4, is_valid: 1 } },
    { prompt_id: "p_foxtrot", category: "general", label: "Foxtrot unchanged",
      prompt_text: "Foxtrot prompt", prompt_redacted: false,
      reference: { text: "foxtrot", length: 7, is_valid: 1 },
      current: { text: "foxtrot", length: 7, is_valid: 1 } },
    { prompt_id: "p_golf", category: "general", label: "Golf same text",
      prompt_text: "Golf prompt", prompt_redacted: false,
      reference: { text: "identical", length: 9, is_valid: 1 },
      current: { text: "identical", length: 12, is_valid: 0 } },
  ],
};

const evaluation = {
  headline: {
    mean_detection_delay_days: 0.97, detection_rate_pct: 100.0,
    evaluated_shift_sigma: 1.2, false_alarms_per_100_days: 0.18,
    overhead_p95_seconds: 0.388, overhead_samples: 12, default_rule: "persist2",
  },
  tradeoff: [
    { rule: "single", mean_detection_delay_days: 0.0, detection_rate_pct: 100.0, false_alarms_per_100_days: 3.09 },
    { rule: "kofn", mean_detection_delay_days: 0.9, detection_rate_pct: 100.0, false_alarms_per_100_days: 1.11 },
    { rule: "persist2", mean_detection_delay_days: 0.97, detection_rate_pct: 100.0, false_alarms_per_100_days: 0.18 },
    { rule: "cusum", mean_detection_delay_days: 0.79, detection_rate_pct: 100.0, false_alarms_per_100_days: 1.58 },
  ],
  power_curves: {
    /* delta values exactly as the API serves them from the CSV: strings */
    mmd_n10: csvRows([[0.0, 0.02], [0.5, 0.6], [1.0, 1.0], [1.5, 1.0], [2.0, 1.0], [3.0, 1.0]]),
    ks_n10: csvRows([[0.0, 0.04], [0.5, 0.0], [1.0, 0.24], [1.5, 0.6], [2.0, 0.82], [3.0, 1.0]]),
    trials_per_point: 50,
  },
  reliability_conditions: [],
};

let routeOverrides = {};

/* The provider comparison fixture is the committed artifact itself, so the
   smoke test also pins that the exported JSON stays renderable. */
const providerComparison = JSON.parse(
  fs.readFileSync(path.join(__dirname, "provider_comparison.json"), "utf8"));

function resp(json, opts = {}) {
  return { ok: opts.ok !== false, status: opts.status || 200,
           headers: { get: () => opts.retryAfter == null ? null : opts.retryAfter },
           json: async () => json };
}

function fixtureResponse(url) {
  requestedUrls.push(url);
  const fn = routeOverrides[url] || getFixture(url);
  if (fn == null) throw new Error("app requested unplanned endpoint " + url);
  return fn();
}

const fixtures = {
  "/health/live": () => resp({ status: "ok" }),
  "/health": () => resp({
    status: "ok", db: "ok", database: "available", db_error: null,
    provider: "gemini-3.1-flash-lite", provider_status: "ok",
    provider_error: null, storage_backend: "sqlite", storage_durable: false,
  }),
  "/config": () => resp({
    target_model: "gemini-3.1-flash-lite", adapter_type: "GeminiAdapter",
    provider_available: true, provider_error: null,
    embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
    tests_start_at_responses: 160, prompt_panel_version: "3.0.0",
    prompt_panel_probes: 40, probes_per_cycle: 40, alpha: 0.05,
    min_consecutive_cycles: 2, category_window_cycles: 3,
    min_samples_per_category: 8, provider_health_ttl_seconds: 60,
    baseline_manifest: {
      provider: "gemini", model_id: "gemini-3.1-flash-lite",
      panel_version: "3.0.0", temperature: "0.0",
      collected_at: "2026-11-09T06:00:00+00:00",
    },
    run_policy: {
      per_visitor_interval_seconds: 600, daily_cycle_budget: 15,
      daily_cycles_used_today: 4,
    },
  }),
  "/stats": () => resp({
    probe_runs: 320, test_results: 12, alerts: 2, cycles: 8,
    successful_probe_runs: 318, failed_probe_runs: 2,
    complete_cycles: 8, evaluated_cycles: 8, awaiting_evaluation_cycles: 0,
  }),
  "/drift/latest": () => resp(latestFull),
  "/cycles?limit=24": () => resp([]),
  "/alerts?limit=10": () => resp([]),
  "/drift/comparison": () => resp({
    ready: false, reference_stored: 120, reference_required: 120,
    current_stored: 40, current_required: 40, tests_start_at: 160, rows: [] }),
  "/drift/categories": () => resp({
    category_window_cycles: 3, min_samples_per_category: 8, rows: [] }),
  "/evaluation": () => resp(evaluation),
  "/static/provider_comparison.json": () => resp(providerComparison),
};

function getFixture(url) {
  return fixtures[url] || null;
}

const requestedUrls = [];
global.fetch = async (url) => fixtureResponse(url);

/* ---------- load the dashboard ---------- */

const script = fs.readFileSync(path.join(__dirname, "app.js"), "utf8");
vm.runInThisContext(script, { filename: "app.js" });

/* ---------- assertions ---------- */

let failures = 0;
function check(name, cond, detail) {
  if (cond) {
    console.log("  pass " + name);
  } else {
    failures += 1;
    console.log("  FAIL " + name + (detail ? " :: " + detail : ""));
  }
}

const settle = () => new Promise((r) => setImmediate(r));

/* The page loads at mount time via ensureAwake(); give the promise chain a
   few ticks, then inspect what rendered. */
(async () => {
  await settle(); await settle(); await settle();

  console.log("scenario 1: healthy endpoints, full data");

  check("header shows API, database and provider as separate states",
    getById("health-pill").innerHTML.includes("API Online")
      && getById("health-pill").className.includes("status-ok")
      && getById("db-pill").innerHTML.includes("Database Available")
      && getById("db-pill").className.includes("status-ok")
      && getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("provider-pill").className.includes("status-ok"),
    `${getById("health-pill").innerHTML} / ${getById("db-pill").innerHTML} / `
      + getById("provider-pill").innerHTML);

  check("readiness (/health) is fetched independently of liveness",
    requestedUrls.some((u) => u === "/health")
      && requestedUrls.some((u) => u === "/health/live"),
    requestedUrls.filter((u) => u.startsWith("/health")).join(", "));

  check("distribution headline follows the distribution family field",
    getById("kpi-drift").textContent === "Shift detected",
    getById("kpi-drift").textContent);

  check("flagged count reads the canonical API field",
    getById("verdict-sub").textContent === "1 of 3 tests flagged",
    getById("verdict-sub").textContent);

  check("verdict meta is the check time only",
    getById("verdict-meta").textContent.startsWith("Last checked ")
      && !getById("verdict-meta").textContent.includes("Reference:"),
    getById("verdict-meta").textContent);

  check("reference and current counts live in their own cells",
    getById("count-ref").textContent === "120"
      && getById("count-cur").textContent === "40",
    `${getById("count-ref").textContent} / ${getById("count-cur").textContent}`);

  check("quality guardrail reported separately and not conflated with shift",
    getById("quality-strip").hidden === false
      && getById("quality-strip").innerHTML.includes("Invalid-response guardrail tripped"),
    getById("quality-strip").innerHTML.slice(0, 80));

  const detHtml = getById("detector-rows").innerHTML;
  check("detector table renders all three rows with methods as secondary text",
    detHtml.includes("Response length") && detHtml.includes("KS")
      && detHtml.includes("Response availability") && detHtml.includes("Fisher / z")
      && detHtml.includes("Semantic shift") && detHtml.includes("Paired MMD")
      && detHtml.includes('class="detector-method"'),
    detHtml.slice(0, 200));
  check("flagged detector shows Flagged with its adjusted p",
    detHtml.includes("Flagged") && detHtml.includes("0.030"),
    detHtml.slice(0, 400));
  check("unflagged detectors show No flag",
    detHtml.includes("No flag"), detHtml.slice(0, 400));
  check("detector decisions are plain text, not chips",
    !detHtml.includes("chip"), detHtml.slice(0, 300));

  check("refusal rate change takes its sign from the delta, not the reference",
    getById("eff-refusal").textContent.startsWith("-5.0"),
    getById("eff-refusal").textContent);

  check("length effect size has an explicit plus for growth",
    getById("eff-length").textContent === "+19.5%",
    getById("eff-length").textContent);

  check("centroid distance renders numerically",
    getById("eff-centroid").textContent === "0.153",
    getById("eff-centroid").textContent);

  /* Empty-history comparison renders a compact bootstrap state. */
  check("fresh deployment state explains the two windows compactly",
    getById("comparison-wrap").innerHTML.includes("Evaluation has not started yet"),
    "");

  check("paired diagnostic derives the exhaustive pattern count",
    getById("paired-patterns").textContent.includes("1024"),
    getById("paired-patterns").textContent);

  check("config shows reference manifest provenance",
    getById("cfg-manifest").textContent.includes("gemini")
      && getById("cfg-manifest").textContent.includes("panel v3.0.0"),
    getById("cfg-manifest").textContent);

  check("config shows provider latency percentiles of the latest cycle",
    getById("cfg-latency").textContent.includes("4.2 s mean")
      && getById("cfg-latency").textContent.includes("5.1 s p95"),
    getById("cfg-latency").textContent);

  check("config shows provider token usage of the latest cycle",
    getById("cfg-tokens").textContent.includes("960"),
    getById("cfg-tokens").textContent);

  check("total probes from /stats stays available in configuration",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("embedding model and baseline manifest survive as secondary metadata",
    getById("cfg-embedding").textContent.includes("all-MiniLM-L6-v2")
      && getById("cfg-manifest").textContent.includes("Baseline: gemini"),
    `${getById("cfg-embedding").textContent} / ${getById("cfg-manifest").textContent}`);

  check("configuration is a collapsed disclosure",
    fs.readFileSync(path.join(__dirname, "index.html"), "utf8")
      .includes('<summary>Configuration</summary>'),
    "");

  check("run note uses the configured cycle size and interval",
    getById("run-note").textContent.includes("40 prompts per run")
      && getById("run-note").textContent.includes("1 check per visitor / 10 min"),
    getById("run-note").textContent);

  check("run note shows the remaining daily quota from /config",
    getById("run-note").textContent.includes("11 of 15 checks left today"),
    getById("run-note").textContent);

  check("threshold and persistence render as compact values, not sentences",
    getById("cfg-alpha").textContent === "0.05 · Benjamini–Hochberg"
      && getById("cfg-persist").textContent === "2 consecutive flagged cycles",
    `${getById("cfg-alpha").textContent} / ${getById("cfg-persist").textContent}`);

  check("no em dash in any rendered UI copy",
    ["kpi-drift", "verdict-sub", "verdict-meta", "run-note", "cfg-model",
     "cfg-panel", "cfg-ref", "cfg-cur", "cfg-alpha", "cfg-persist",
     "cfg-last-run", "cfg-latency", "cfg-tokens", "cfg-embedding",
     "cfg-manifest", "eff-length", "eff-centroid", "eff-refusal",
     "detector-rows", "activity-list", "alerts-list"]
      .every((id) => !getById(id).textContent.includes("—")
        && !getById(id).innerHTML.includes("—")),
    "");

  check("header model label is an understated model name",
    getById("model-label").textContent === "Gemini 3.1 Flash-Lite"
      && getById("model-label").className === "brand-model",
    getById("model-label").textContent);

  check("header groups status with subtle vertical dividers",
    fs.readFileSync(path.join(__dirname, "index.html"), "utf8")
      .includes('class="status-sep"')
      && fs.readFileSync(path.join(__dirname, "app.css"), "utf8")
        .includes(".status-sep"),
    "");

  console.log("scenario 1b: activity table, stored detector history, alerts");

  routeOverrides = {
    "/cycles?limit=24": () => resp(cycleRows),
    "/alerts?limit=10": () => resp([storedAlert]),
  };
  await loadAll();
  await settle();

  const actHtml = getById("activity-list").innerHTML;
  check("activity is a real table with cycle, probes, status and outcome",
    actHtml.includes('class="activity-table"')
      && actHtml.includes("<th>Cycle</th>")
      && actHtml.includes("<th>Time</th>")
      && actHtml.includes("<th class=\"num\">Probes</th>")
      && actHtml.includes("<th>Status</th>")
      && actHtml.includes("<th>Detector outcome</th>"),
    actHtml.slice(0, 260));
  check("activity shows exactly the five most recent cycles",
    (actHtml.match(/data-testid="activity-row"/g) || []).length === 5,
    actHtml.slice(0, 120));
  check("activity is newest first and drops the oldest cycle",
    actHtml.indexOf("#6") < actHtml.indexOf("#5")
      && actHtml.indexOf("#5") < actHtml.indexOf("#4")
      && actHtml.indexOf("#4") < actHtml.indexOf("#3")
      && actHtml.indexOf("#3") < actHtml.indexOf("#2")
      && !actHtml.includes("#1"),
    actHtml.slice(0, 300));
  check("activity names probe count and shift outcome without badges",
    actHtml.includes("No shift") && actHtml.includes("Shift flagged")
      && actHtml.includes(">40<") && !actHtml.includes("chip"),
    actHtml.slice(0, 300));
  check("an incomplete cycle is marked Interrupted",
    /#4[\s\S]*?Interrupted/.test(actHtml),
    actHtml.slice(0, 200));
  check("availability change is part of the detector outcome",
    /#3[\s\S]*?Availability changed/.test(actHtml),
    actHtml.slice(0, 320));
  check("a stored alert decorates its cycle only",
    /#3[\s\S]*?1 alert/.test(actHtml)
      && !/#2[\s\S]*?1 alert/.test(actHtml),
    actHtml.slice(0, 400));
  check("activity header counts the cycles shown",
    getById("activity-count").textContent === "5 of 6 cycles",
    getById("activity-count").textContent);

  const histHtml = getById("history-chart").innerHTML;
  check("detector history is browser-rendered svg",
    histHtml.includes("<svg") && histHtml.includes('class="chart"'),
    histHtml.slice(0, 160));
  check("history plots all three stored detector series",
    histHtml.includes(">KS<") && histHtml.includes(">Availability<")
      && histHtml.includes(">Paired MMD<"),
    histHtml.slice(-320));
  check("history uses distinct marker shapes per series",
    histHtml.includes("mk-circle") && histHtml.includes("mk-square")
      && histHtml.includes("mk-triangle")
      && histHtml.includes("<rect") && histHtml.includes("<polygon"),
    histHtml.slice(-320));
  check("history draws the dashed alpha threshold",
    histHtml.includes('stroke-dasharray="4 3"') && histHtml.includes("alpha 0.05"),
    "");
  check("history labels cycles in chronological order",
    histHtml.indexOf("#1<") < histHtml.indexOf("#2<")
      && histHtml.indexOf("#2<") < histHtml.indexOf("#3<")
      && histHtml.indexOf("#3<") < histHtml.indexOf("#4<")
      && histHtml.indexOf("#4<") < histHtml.indexOf("#5<")
      && histHtml.indexOf("#5<") < histHtml.indexOf("#6<"),
    "");
  check("a missing adjusted p leaves a gap, not a zero point",
    (histHtml.match(/<title>Cycle #/g) || []).length === 17
      && !histHtml.includes("adjusted p 0.000"),
    String((histHtml.match(/<title>Cycle #/g) || []).length));
  check("the latest cycle has a dashed guide and a halo point",
    histHtml.includes('stroke-dasharray="2 3"')
      && histHtml.includes('r="5.4"')
      && histHtml.includes("latest cycle"),
    "");
  check("a flagged cycle is ringed in red",
    histHtml.includes('stroke="#DC2626"'),
    "");
  check("history tooltips carry cycle, timestamp, detector and flag state",
    histHtml.includes("Cycle #3") && histHtml.includes("adjusted p 0.022")
      && histHtml.includes("(flagged)"),
    "");
  check("history note labels the source as evaluated stored cycles",
    getById("history-note").textContent.includes("6 evaluated cycles")
      && getById("history-note").textContent.includes("stored detector results"),
    getById("history-note").textContent);

  const alertsHtml = getById("alerts-list").innerHTML;
  check("alerts panel renders the persisted alert",
    alertsHtml.includes("Semantic shift persisted")
      && getById("alert-count").textContent === "1 recent",
    alertsHtml.slice(0, 160));

  routeOverrides = {
    "/drift/comparison": () => resp(samplesReady),
  };
  await loadAll();
  await settle();

  const samplesHtml = getById("comparison-wrap").innerHTML;
  check("collapsed summary counts the stored prompts",
    getById("sample-count").textContent === "· 8 prompts",
    getById("sample-count").textContent);
  check("samples preview five rows and count the rest",
    samplesHtml.includes("5 of 8 shown")
      && (samplesHtml.match(/class="sample-row"/g) || []).length === 5,
    samplesHtml.slice(0, 160));
  check("changed responses are sorted before unchanged ones",
    samplesHtml.indexOf("Bravo changed") < samplesHtml.indexOf("Delta changed")
      && samplesHtml.indexOf("Delta changed") < samplesHtml.indexOf("Alpha unchanged")
      && samplesHtml.indexOf("Delta changed") < samplesHtml.indexOf("Charlie unchanged"),
    samplesHtml.slice(0, 400));
  check("sample rows use the aligned Prompt Reference Current Change grid",
    samplesHtml.includes("sample-head")
      && samplesHtml.includes("<span>Prompt</span>")
      && samplesHtml.includes("<span>Reference</span>")
      && samplesHtml.includes("<span>Current</span>")
      && samplesHtml.includes("<span>Change</span>"),
    samplesHtml.slice(0, 220));
  check("row values carry character counts and 16px validity icons",
    samplesHtml.includes("11 ch") && samplesHtml.includes('class="s-icon s-ok"')
      && samplesHtml.includes('width="16" height="16"')
      && samplesHtml.includes('aria-label="Non-empty response"')
      && samplesHtml.includes("+1 ch"),
    samplesHtml.slice(0, 400));
  check("redacted samples keep the withheld marker and never reveal text",
    samplesHtml.includes("details withheld") && samplesHtml.includes("withheld")
      && !samplesHtml.includes("(withheld: safety boundary prompt)</code>"),
    samplesHtml.slice(0, 400));
  check("the full text lives in the expandable detail, not the row",
    samplesHtml.includes('class="sample-detail"')
      && samplesHtml.includes("Safety boundary probe"),
    "");
  check("expanded reference and current responses repeat the validity icons",
    (samplesHtml.match(/class="resp-line"/g) || []).length >= 8
      && samplesHtml.includes('class="resp-icon"')
      && samplesHtml.includes('aria-label="Non-empty response"'),
    String((samplesHtml.match(/class="resp-line"/g) || []).length));

  const toggle = getById("samples-toggle");
  check("Show all names the remaining count",
    samplesHtml.includes("Show all 8") && toggle.listeners.click != null,
    samplesHtml.slice(-160));
  toggle.listeners.click[0]();
  const allHtml = getById("comparison-wrap").innerHTML;
  check("Show all reveals every prompt and can be collapsed again",
    allHtml.includes("8 of 8 shown") && allHtml.includes("Foxtrot unchanged")
      && allHtml.includes("Show fewer"),
    allHtml.slice(0, 160));
  check("identical text stays unchanged even when stored lengths disagree",
    /Golf same text[\s\S]*?unchanged/.test(allHtml),
    "");
  check("empty and unrecorded validity use distinct accessible icons",
    allHtml.includes('class="s-icon s-bad"')
      && allHtml.includes('aria-label="Empty response"')
      && allHtml.includes('class="s-icon s-neutral"')
      && allHtml.includes('aria-label="Validity not recorded"'),
    allHtml.slice(0, 300));

  const select = getById("samples-category");
  check("category filter lists the stored categories",
    allHtml.includes("All categories")
      && allHtml.includes('value="code"') && allHtml.includes('value="general"'),
    "");
  select.listeners.change[0]({ target: { value: "code" } });
  const codeHtml = getById("comparison-wrap").innerHTML;
  check("category filter narrows the rows",
    codeHtml.includes("2 of 2 shown") && !codeHtml.includes("Alpha unchanged"),
    codeHtml.slice(0, 160));

  vmEval('samplesShowAll = false; samplesCategory = "all";');
  routeOverrides = {};

  console.log("scenario 2: evaluation tab data fidelity");

  /* Evaluation loads lazily via the tab; the fixture answers directly. */
  await loadEvaluation();
  await settle();

  /* The core regression: CSV string keys must resolve through the numeric
     lookup, and zero power rows must be visible. */
  const powerHtml = getById("eval-power").innerHTML;
  check("MMD 100% renders (numeric lookup past string keys)",
    powerHtml.includes("100%"), powerHtml.slice(0, 200));
  check("KS 0% power row is displayed, not swallowed",
    powerHtml.includes("0%"), powerHtml.slice(0, 200));
  check("MMD 60% row matches the CSV",
    powerHtml.includes("60%"), "");
  check("KS 24% row matches the CSV",
    powerHtml.includes("24%"), "");
  check("power chart is drawn as svg",
    getById("eval-power-chart").innerHTML.includes("<svg"), "");
  check("trade-off chart is drawn as svg",
    getById("eval-tradeoff-chart").innerHTML.includes("<svg"), "");

  const tradeoffHtml = getById("eval-tradeoff").innerHTML;
  check("persist2 marked shipped in the trade-off table",
    tradeoffHtml.includes("shipped"), "");

  /* Headline numbers stay faithful. */
  check("overhead renders with the sample count basis",
    getById("ev-overhead").textContent === "388 ms",
    getById("ev-overhead").textContent);

  check("methodology disclosure states the limited trial basis",
    getById("ev-methodology").innerHTML.includes("limited"),
    getById("ev-methodology").innerHTML.slice(0, 200));

  console.log("scenario 2a: one malformed evaluation block cannot erase the rest");
  routeOverrides = { "/evaluation": () => resp({
    ...evaluation,
    power_curves: { mmd_n10: "not-an-array", ks_n10: null, trials_per_point: 50 },
  }) };
  await loadEvaluation();
  await settle();
  check("trade-off chart still renders when power data is malformed",
    getById("eval-tradeoff-chart").innerHTML.includes("<svg")
      && getById("eval-tradeoff").innerHTML.includes("persist2"),
    getById("eval-tradeoff-chart").innerHTML.slice(0, 120));
  check("malformed power block states unavailable instead of crashing",
    getById("eval-power-chart").innerHTML.includes("unavailable"),
    getById("eval-power-chart").innerHTML.slice(0, 120));
  routeOverrides = {};
  await loadEvaluation();
  await settle();
  check("evaluation charts recover after the malformed payload",
    getById("eval-power-chart").innerHTML.includes("<svg"),
    getById("eval-power-chart").innerHTML.slice(0, 120));

  console.log("scenario 2b: recorded provider comparison renders from the committed artifact");

  await settle();

  const provCmpHtml = getById("prov-cmp").innerHTML;
  check("both provider models render in the comparison table",
    provCmpHtml.includes("openai/gpt-oss-20b")
      && provCmpHtml.includes("gemini-3.1-flash-lite"),
    provCmpHtml.slice(0, 200));
  check("groq latency range renders from the artifact",
    provCmpHtml.includes("0.69") && provCmpHtml.includes("1.23"),
    provCmpHtml.slice(0, 400));
  check("groq reference validity is shown as not measured, not invented",
    provCmpHtml.includes("not measured"),
    provCmpHtml.slice(0, 600));

  const provVerdict = getById("prov-cmp-verdict").innerHTML;
  check("comparison verdict reports no corrected flag and no alert",
    provVerdict.includes("No corrected test flagged") && provVerdict.includes("no alert fired"),
    provVerdict.slice(0, 160));
  check("comparison carries the paired diagnostic and differences",
    provVerdict.includes("matched prompts") && provVerdict.includes("reasoning"),
    provVerdict.slice(0, 300));
  check("comparison states the cross-provider caveat explicitly",
    provVerdict.includes("Cross-provider") || provVerdict.includes("cross-provider"),
    provVerdict.slice(-300));
  check("comparison cites its source artifacts",
    provVerdict.includes("real_provider_results.json"),
    provVerdict.slice(-300));

  console.log("scenario 2c: comparison asset failure is an explicit state");
  routeOverrides = { "/static/provider_comparison.json": () => {
    throw new Error("asset missing");
  } };
  vmEval("state.providerComparison = null;");
  await loadProviderComparison();
  await settle();
  check("missing comparison asset renders an explicit unavailable state",
    getById("prov-cmp").innerHTML.includes("unavailable"),
    getById("prov-cmp").innerHTML.slice(0, 120));
  routeOverrides = {};

  console.log("scenario 3: liveness failure is not API degradation when data answers");

  /* Successful data endpoints are the stronger evidence. A failed liveness
     probe must not contradict the activity table on screen. */
  routeOverrides = { "/health/live": () => {
    throw new Error("connection refused");
  } };
  vmEval("state.live = null; loadLive();");
  await settle();

  check("successful data keeps a failed liveness probe at API Online",
    getById("health-pill").innerHTML.includes("API Online")
      && !getById("health-pill").innerHTML.includes("Degraded"),
    getById("health-pill").innerHTML);

  check("provider availability is unaffected by a liveness probe failure",
    getById("provider-pill").innerHTML.includes("Gemini Available"),
    getById("provider-pill").innerHTML);

  check("database availability is unaffected by a liveness probe failure",
    getById("db-pill").innerHTML.includes("Database Available"),
    getById("db-pill").innerHTML);

  await loadAll();
  await settle();

  check("stored responses still render with liveness failing",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("detectors still render with liveness failing",
    getById("detector-rows").innerHTML.includes("Flagged"),
    "");

  /* Before any data endpoint answers, a failed liveness probe is unknown,
     not offline. Offline needs both signals to fail. */
  vmEval('state.dataOk = false; state.dataFailed = false; '
    + 'state.live = { status: "unreachable" }; renderHealthPill();');
  check("liveness failure before data answers is API Unknown",
    getById("health-pill").innerHTML.includes("API Unknown"),
    getById("health-pill").innerHTML);

  vmEval('state.dataFailed = true; renderHealthPill();');
  check("API Offline only when data endpoints failed too",
    getById("health-pill").innerHTML.includes("API Offline"),
    getById("health-pill").innerHTML);

  vmEval('state.live = { status: "ok" }; state.dataOk = false; '
    + 'state.dataFailed = true; renderHealthPill();');
  check("liveness alone with failing data is API Degraded, not fully online",
    getById("health-pill").innerHTML.includes("API Degraded"),
    getById("health-pill").innerHTML);

  routeOverrides = {};
  vmEval("state.dataOk = true; state.dataFailed = false;");
  await loadLive();
  await settle();

  console.log("scenario 3a: database and provider readiness are independent");

  routeOverrides = { "/health": () => resp({
    status: "degraded", db: "unavailable", database: "unavailable",
    db_error: "disk I/O error", provider_status: "ok", provider_error: null,
    provider: "gemini-3.1-flash-lite",
  }) };
  vmEval("state.health = null; state.healthCheckedAt = 0; loadHealth();");
  await settle();
  check("database failure does not rewrite provider availability",
    getById("db-pill").innerHTML.includes("Database Unavailable")
      && getById("db-pill").className.includes("status-bad")
      && getById("provider-pill").innerHTML.includes("Gemini Available"),
    `${getById("db-pill").innerHTML} / ${getById("provider-pill").innerHTML}`);

  routeOverrides = { "/health": () => resp({
    status: "degraded", db: "ok", database: "available", db_error: null,
    provider_status: "unreachable", provider_error: "quota exhausted",
    provider: "gemini-3.1-flash-lite",
  }) };
  vmEval("state.health = null; state.healthCheckedAt = 0; loadHealth();");
  await settle();
  check("provider failure does not rewrite database availability",
    getById("provider-pill").innerHTML.includes("Gemini Unavailable")
      && getById("provider-pill").className.includes("status-bad")
      && getById("db-pill").innerHTML.includes("Database Available"),
    `${getById("db-pill").innerHTML} / ${getById("provider-pill").innerHTML}`);

  routeOverrides = {};
  vmEval("state.health = null; state.healthCheckedAt = 0; loadHealth();");
  await settle();

  console.log("scenario 3b: provider failure is visible without rewriting API state");

  routeOverrides = {
    "/config": () => resp({
      target_model: "gemini-3.1-flash-lite", adapter_type: "GeminiAdapter",
      provider_available: false, provider_error: "quota exhausted",
      embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
      prompt_panel_version: "3.0.0", probes_per_cycle: 40, alpha: 0.05,
      min_consecutive_cycles: 2,
    }),
    "/health": () => resp({
      status: "degraded", db: "ok", database: "available",
      provider_status: "unreachable", provider_error: "quota exhausted",
      provider: "gemini-3.1-flash-lite",
    }),
  };
  vmEval("state.health = null; state.healthCheckedAt = 0;");
  await loadAll();
  await settle();

  check("provider failure appears as Gemini Unavailable, not as a model suffix",
    getById("provider-pill").innerHTML.includes("Gemini Unavailable")
      && getById("model-label").textContent === "Gemini 3.1 Flash-Lite",
    `${getById("provider-pill").innerHTML} / ${getById("model-label").textContent}`);
  check("provider failure does not rewrite the API state",
    getById("health-pill").innerHTML.includes("API Online"),
    getById("health-pill").innerHTML);

  /* Without credentials the API cannot construct the adapter, so /config
     serves no target_model. Readiness still knows the configured model;
     the header must not go blank. */
  routeOverrides = {
    "/config": () => resp({
      target_model: null, adapter_type: null,
      provider_available: false,
      provider_error: "Gemini API key is required.",
      embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
      prompt_panel_version: "3.0.0", probes_per_cycle: 40, alpha: 0.05,
      min_consecutive_cycles: 2,
    }),
    "/health": () => resp({
      status: "degraded", db: "ok", database: "available",
      provider_status: "not_configured",
      provider_error: "Gemini API key is required.",
      provider: "gemini-3.1-flash-lite",
    }),
  };
  vmEval("state.health = null; state.healthCheckedAt = 0; state.config = null;");
  await loadAll();
  vmEval("loadHealth();");
  await settle();
  check("header names the model when /config cannot build the adapter",
    getById("model-label").textContent === "Gemini 3.1 Flash-Lite"
      && getById("provider-pill").innerHTML.includes("Gemini Unavailable"),
    `${getById("model-label").textContent} / ${getById("provider-pill").innerHTML}`);
  check("configuration panel names the same model instead of null",
    getById("cfg-model").textContent === "gemini-3.1-flash-lite",
    getById("cfg-model").textContent);
  vmEval("state.health = null; state.healthCheckedAt = 0; state.config = null;");

  routeOverrides = {};
  await loadAll();
  await settle();

  console.log("scenario 3c: baseline cycle roles, empty responses and storage identity");

  /* The committed Gemini baseline is three reference cycles plus one
     evaluated cycle. Baseline cycles are not pending evaluations, and their
     empty responses are not provider failures. */
  const oneResult = {
    id: 1, timestamp: "2026-11-10T18:22:00",
    overall_drift_detected: 0, overall_quality_degradation: 0,
    ks_adjusted_pvalue: 0.82, ks_significant: 0,
    proportion_z_adjusted_pvalue: 0.64, proportion_z_significant: 0,
    mmd_adjusted_pvalue: 1.0, mmd_significant: 0,
  };
  const baselineCycles = [
    { cycle_id: "evaluated", cycle_number: 4, probe_count: 40, empty_count: 0,
      failed_count: 0, status: "evaluated", reference_baseline: false,
      started_at: "2026-11-10T15:27:00", ended_at: "2026-11-10T15:27:35",
      result: oneResult },
    { cycle_id: "base-3", cycle_number: 3, probe_count: 40, empty_count: 1,
      failed_count: 0, status: "reference_baseline", reference_baseline: true,
      started_at: "2026-11-09T16:54:00", ended_at: "2026-11-09T16:54:19",
      result: null },
    { cycle_id: "base-2", cycle_number: 2, probe_count: 40, empty_count: 1,
      failed_count: 0, status: "reference_baseline", reference_baseline: true,
      started_at: "2026-11-09T16:44:00", ended_at: "2026-11-09T16:44:04",
      result: null },
    { cycle_id: "base-1", cycle_number: 1, probe_count: 40, empty_count: 1,
      failed_count: 0, status: "reference_baseline", reference_baseline: true,
      started_at: "2026-11-09T13:00:00", ended_at: "2026-11-09T13:00:13",
      result: null },
  ];
  routeOverrides = {
    "/cycles?limit=24": () => resp(baselineCycles),
    "/config": () => resp({
      target_model: "gemini-3.1-flash-lite", adapter_type: "GeminiAdapter",
      provider_available: true, provider_error: null,
      embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
      prompt_panel_version: "3.0.0", probes_per_cycle: 40, alpha: 0.05,
      min_consecutive_cycles: 2,
      storage: { backend: "sqlite", durable: false, location: "/app/data/seed_gemini.db",
        note: "SQLite on the container filesystem." },
    }),
    "/stats": () => resp({
      probe_runs: 160, test_results: 1, cycles: 4, evaluated_cycles: 1,
      reference_baseline_cycles: 3, awaiting_evaluation_cycles: 0,
      failed_cycles: 0, complete_cycles: 4, alerts: 0,
    }),
  };
  await loadAll();
  await settle();

  const baseHtml = getById("activity-list").innerHTML;
  check("baseline cycles are labelled Reference baseline, not awaiting evaluation",
    (baseHtml.match(/Reference baseline/g) || []).length === 3
      && !baseHtml.includes("Awaiting evaluation"),
    baseHtml.slice(0, 400));
  check("only the real detector result is shown as Evaluated",
    (baseHtml.match(/Evaluated/g) || []).length === 1, baseHtml.slice(0, 400));
  check("empty responses are described as empty, not failed",
    baseHtml.includes("1 empty response") && !baseHtml.includes("failed"),
    baseHtml.slice(0, 400));
  check("one evaluated cycle renders a compact one-point history svg",
    getById("history-chart").innerHTML.includes("<svg")
      && getById("history-note").textContent.includes("1 evaluated cycle available")
      && !getById("history-note").textContent.includes("Awaiting"),
    getById("history-note").textContent);
  check("SQLite storage is shown as ephemeral, never durable",
    getById("cfg-storage").textContent === "SQLite · Ephemeral"
      && !getById("cfg-storage").textContent.includes("Persistent"),
    getById("cfg-storage").textContent);

  /* A failed /health fetch must not turn Database Available into Unknown
     while /stats and /cycles have already queried storage successfully. */
  routeOverrides = {
    "/health": () => resp({ detail: "temporary readiness failure" },
      { ok: false, status: 503 }),
  };
  vmEval("state.health = null; state.healthCheckedAt = 0; loadHealth();");
  await settle();
  check("database stays Available when /health fails but /stats answered",
    getById("db-pill").innerHTML.includes("Database Available")
      && !getById("db-pill").innerHTML.includes("Unknown"),
    getById("db-pill").innerHTML);
  vmEval("globalThis.__healthError = state.healthError;");
  const healthErr = globalThis.__healthError;
  check("readiness failure records HTTP status as a diagnostic",
    Boolean(healthErr && healthErr.httpStatus === 503
      && healthErr.timeout === false && healthErr.detail),
    JSON.stringify(healthErr));

  /* An unchecked provider is unknown, not a measured failure. */
  routeOverrides = { "/health": () => resp({
    status: "degraded", db: "ok", database: "available",
    provider_status: "unchecked", provider_error: null,
    provider: "gemini-3.1-flash-lite",
  }) };
  vmEval("state.health = null; state.healthCheckedAt = 0; loadHealth();");
  await settle();
  check("unchecked provider is Unknown, not Unavailable",
    getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && !getById("provider-pill").innerHTML.includes("Gemini Unavailable"),
    getById("provider-pill").innerHTML);

  console.log("scenario 3d: readiness request outcomes explain the provider pill");

  const healthyHealth = () => resp({
    status: "ok", db: "ok", database: "available", db_error: null,
    provider: "gemini-3.1-flash-lite", provider_status: "ok",
    provider_error: null,
    diagnostics: { timeout: false, db_ready: true, provider_ready: true,
      error_type: null, error_detail: null },
  });
  const uncheckedHealth = () => resp({
    status: "degraded", db: "ok", database: "available", db_error: null,
    provider: "gemini-3.1-flash-lite", provider_status: "unchecked",
    provider_error: null,
  });
  const reloadHealth = () => vmEval(
    "state.health = null; state.healthCheckedAt = 0; loadHealth();");

  routeOverrides = { "/health": healthyHealth };
  reloadHealth();
  await settle();
  check("200 with provider_status ok is Gemini Available",
    getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("provider-pill").className.includes("status-ok")
      && getById("provider-pill").title.includes("answered its readiness ping")
      && getById("model-label").title === getById("provider-pill").title,
    `${getById("provider-pill").innerHTML} / ${getById("provider-pill").title}`);

  routeOverrides = { "/health": uncheckedHealth };
  reloadHealth();
  await settle();
  check("unchecked is Gemini Unknown with the reason in both tooltips",
    getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && !getById("provider-pill").innerHTML.includes("Gemini Unavailable")
      && getById("provider-pill").title.includes("provider_status 'unchecked'")
      && getById("model-label").title === getById("provider-pill").title,
    getById("provider-pill").title);

  routeOverrides = { "/health": () => resp(
    { detail: "Service Unavailable" }, { ok: false, status: 503 }) };
  reloadHealth();
  await settle();
  check("HTTP 503 is Gemini Unknown and shows the actual status",
    getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && getById("provider-pill").title.includes("HTTP 503")
      && getById("model-label").title === getById("provider-pill").title,
    getById("provider-pill").title);
  check("readiness HTTP failure does not rewrite database availability",
    getById("db-pill").innerHTML.includes("Database Available"),
    getById("db-pill").innerHTML);

  routeOverrides = { "/health": () => {
    const err = new Error("deadline exceeded");
    err.name = "AbortError";
    throw err;
  } };
  reloadHealth();
  await settle();
  check("timed-out readiness is Gemini Unknown and shows the timeout",
    getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && getById("provider-pill").title.includes("timed out")
      && getById("model-label").title === getById("provider-pill").title,
    getById("provider-pill").title);

  routeOverrides = { "/health": healthyHealth };
  reloadHealth();
  await settle();
  vmEval("globalThis.__recoveredHealth = state.healthError;");
  check("a subsequent healthy response recovers Gemini Available",
    getById("provider-pill").innerHTML.includes("Gemini Available")
      && globalThis.__recoveredHealth === null,
    `${getById("provider-pill").innerHTML} / ${JSON.stringify(globalThis.__recoveredHealth)}`);

  console.log("scenario 3e: readiness refresh uses state, not only HTTP caching");

  /* The frontend refresh interval is separate from Cache-Control. Test the
     loadHealth() cache itself: unresolved states must be retried, settled
     states may be reused until the backend ping TTL elapses. */
  const healthRequestCount = () => requestedUrls.filter((u) => u === "/health").length;
  const unavailableHealth = () => resp({
    status: "degraded", db: "ok", database: "available", db_error: null,
    provider: "gemini-3.1-flash-lite", provider_status: "unreachable",
    provider_error: "quota exhausted",
  });
  const brokenDbHealth = () => resp({
    status: "degraded", db: "unavailable", database: "unavailable",
    db_error: "disk I/O error",
    provider: "gemini-3.1-flash-lite", provider_status: "ok",
    provider_error: null,
  });
  vmEval("state.config.provider_health_ttl_seconds = 60; "
    + "globalThis.__healthRefreshMs = healthRefreshMs();");
  check("frontend TTL follows the backend provider ping TTL",
    globalThis.__healthRefreshMs === 60000,
    String(globalThis.__healthRefreshMs));

  /* A fresh `unchecked` result must not freeze the indicator. */
  routeOverrides = { "/health": uncheckedHealth };
  reloadHealth();
  await settle();
  const afterUnchecked = healthRequestCount();
  routeOverrides = { "/health": healthyHealth };
  vmEval("loadHealth();");   // no time travel: still inside the cache interval
  await settle();
  check("unchecked is retried immediately when the provider is healthy",
    healthRequestCount() === afterUnchecked + 1
      && getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("model-label").title === getById("provider-pill").title,
    `${healthRequestCount()} / ${getById("provider-pill").innerHTML}`);

  /* A failed request leaves state.health null; the next refresh must still
     recover without waiting for a five-minute cache window. */
  routeOverrides = { "/health": () => resp(
    { detail: "temporary failure" }, { ok: false, status: 503 }) };
  reloadHealth();
  await settle();
  const afterFailure = healthRequestCount();
  check("HTTP failure is Unknown with the status visible",
    getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && getById("provider-pill").title.includes("HTTP 503"),
    getById("provider-pill").title);
  routeOverrides = { "/health": healthyHealth };
  vmEval("loadHealth();");
  await settle();
  check("healthy response after an HTTP failure is fetched and applied",
    healthRequestCount() === afterFailure + 1
      && getById("provider-pill").innerHTML.includes("Gemini Available"),
    `${healthRequestCount()} / ${getById("provider-pill").innerHTML}`);

  /* A decisive failure may be reused until the TTL elapses, then flips. */
  routeOverrides = { "/health": unavailableHealth };
  reloadHealth();
  await settle();
  check("unreachable is Gemini Unavailable with the provider error",
    getById("provider-pill").innerHTML.includes("Gemini Unavailable")
      && getById("provider-pill").title.includes("quota exhausted")
      && getById("model-label").title === getById("provider-pill").title,
    getById("provider-pill").title);
  const afterUnreachable = healthRequestCount();
  routeOverrides = { "/health": healthyHealth };
  vmEval("loadHealth();");
  await settle();
  check("settled failure is reused inside the TTL, without a provider call",
    healthRequestCount() === afterUnreachable
      && getById("provider-pill").innerHTML.includes("Gemini Unavailable"),
    `${healthRequestCount()} / ${getById("provider-pill").innerHTML}`);
  vmEval("state.healthCheckedAt = Date.now() - healthRefreshMs() - 1; loadHealth();");
  await settle();
  check("unreachable transitions to ok once the TTL has elapsed",
    healthRequestCount() === afterUnreachable + 1
      && getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("model-label").title === getById("provider-pill").title,
    `${healthRequestCount()} / ${getById("provider-pill").innerHTML}`);

  const afterHealthy = healthRequestCount();
  vmEval("loadHealth();");
  await settle();
  check("repeated refresh inside the TTL reuses the healthy result",
    healthRequestCount() === afterHealthy
      && getById("provider-pill").innerHTML.includes("Gemini Available"),
    `${healthRequestCount()} / ${getById("provider-pill").innerHTML}`);

  /* Provider status comes only from provider_status, never from db_ok. */
  routeOverrides = { "/health": brokenDbHealth };
  reloadHealth();
  await settle();
  check("database failure does not change provider availability",
    getById("db-pill").innerHTML.includes("Database Unavailable")
      && getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("model-label").title === getById("provider-pill").title,
    `${getById("db-pill").innerHTML} / ${getById("provider-pill").innerHTML}`);

  routeOverrides = {};
  await loadAll();
  await settle();

  console.log("scenario 3f: a finished run refreshes stale readiness");

  /* Regression for the deployed mismatch: runCheck() used to end with only
     loadAll(). That re-rendered an unresolved health snapshot while the API
     had already recovered, so the data cycle looked complete but the pill
     stayed Unknown. Reproduce the old completion call first. */
  routeOverrides = { "/health": healthyHealth };
  vmEval(`state.health = {
    status: 'degraded', db: 'ok', database: 'available', db_error: null,
    provider: 'gemini-3.1-flash-lite', provider_status: 'unchecked',
    provider_error: null
  };
  state.healthError = null;
  state.healthCheckedAt = Date.now();
  renderStatusGroup(); renderModelLabel();`);
  const beforeCycle = healthRequestCount();
  await loadAll();
  await settle();
  check("data-only completion reproduces stale Gemini Unknown",
    healthRequestCount() === beforeCycle
      && getById("provider-pill").innerHTML.includes("Gemini Unknown")
      && getById("model-label").title === getById("provider-pill").title,
    `${healthRequestCount() - beforeCycle} / ${getById("provider-pill").innerHTML}`);

  const beforeRunRefresh = healthRequestCount();
  await refreshAfterRun();
  await settle();
  check("finished run fetches fresh readiness and recovers Gemini Available",
    healthRequestCount() === beforeRunRefresh + 1
      && getById("provider-pill").innerHTML.includes("Gemini Available")
      && getById("provider-pill").title.includes("answered its readiness ping")
      && getById("model-label").title === getById("provider-pill").title,
    `${healthRequestCount() - beforeRunRefresh} / ${getById("provider-pill").innerHTML}`);

  /* A fresh settled verdict is still reused: completing another run must
     not spend provider quota on an unnecessary ping. */
  const afterRunRefresh = healthRequestCount();
  await refreshAfterRun();
  await settle();
  check("a second finished run inside the TTL makes no provider ping",
    healthRequestCount() === afterRunRefresh
      && getById("provider-pill").innerHTML.includes("Gemini Available"),
    `${healthRequestCount() - afterRunRefresh} / ${getById("provider-pill").innerHTML}`);

  routeOverrides = {};
  await loadAll();
  await settle();

  /* PostgreSQL with a durable backend is the only persistent claim. */
  routeOverrides = { "/config": () => resp({
    target_model: "gemini-3.1-flash-lite", adapter_type: "GeminiAdapter",
    provider_available: true, provider_error: null,
    embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
    prompt_panel_version: "3.0.0", probes_per_cycle: 40, alpha: 0.05,
    min_consecutive_cycles: 2,
    storage: { backend: "postgres", durable: true, location: "DATABASE_URL",
      note: "Managed PostgreSQL. Verify the plan is non-expiring." },
  }) };
  await loadAll();
  await settle();
  check("configured PostgreSQL is shown as persistent",
    getById("cfg-storage").textContent === "PostgreSQL · Persistent",
    getById("cfg-storage").textContent);

  /* A configured PostgreSQL URL that fails to initialize is a read-only
     SQLite fallback: the dashboard still renders stored data, but it must not
     offer to start a new cycle or claim durable history. */
  const baseConfig = await fixtures["/config"]().json();
  routeOverrides = { "/config": () => resp({
    ...baseConfig,
    storage: {
      backend: "sqlite-fallback", durable: false,
      location: "/app/data/drift_monitor.db",
      note: "DATABASE_URL is configured but PostgreSQL is unavailable.",
      error: "connection failed with api_key=<redacted>",
      fallback: true, accepts_writes: false, new_checks_enabled: false,
    },
  }) };
  await loadAll();
  await settle();
  check("read-only fallback disables Run check and explains why",
    getById("run-btn").disabled === true
      && getById("run-note").textContent.includes("New checks are disabled")
      && getById("run-note").textContent.includes("DATABASE_URL"),
    `${getById("run-btn").disabled} / ${getById("run-note").textContent}`);
  check("read-only fallback is never shown as persistent",
    getById("cfg-storage").textContent === "SQLite · Ephemeral"
      && getById("db-pill").title.includes("New checks are disabled"),
    `${getById("cfg-storage").textContent} / ${getById("db-pill").title}`);

  getById("run-note").dataset.active = "";
  routeOverrides = {};
  await loadAll();
  await settle();

  console.log("scenario 4: split signals, redacted prompts, rate limits");

  /* availability fires, distribution does not: the headline must stay
     "No shift" and the quality strip must name the real source. */
  routeOverrides = { "/drift/latest": () => resp({
    ...latestFull,
    drift_detected: 0, distribution_shift_detected: 0,
    ks_significant: 0, mmd_significant: 0,
    quality_change_detected: 1, proportion_z_significant: 1,
    quality_degradation: 0,
  }) };
  await loadAll();
  await settle();

  check("availability flag cannot flip the distribution headline",
    getById("kpi-drift").textContent === "No shift",
    getById("kpi-drift").textContent);

  check("availability change is reported as its own quality signal",
    getById("quality-strip").hidden === false
      && getById("quality-strip").innerHTML.includes("Response availability changed"),
    getById("quality-strip").innerHTML.slice(0, 120));

  routeOverrides = {};

  /* Redacted comparison: the safety boundary row shows only the public
     label plus the withheld marker, and the expanded body explains the
     withholding instead of revealing the answer. */
  routeOverrides = { "/drift/comparison": () => resp({
    ready: true, reference_size: 120, current_size: 40,
    rows: [
      { prompt_id: "code_017", category: "code",
        label: "Palindrome check function",
        prompt_text: "Write a function that checks whether a string is a palindrome.",
        prompt_redacted: false,
        reference: { text: "def is_palindrome(s): ...", length: 60, is_valid: 1, is_refusal: 0 },
        current: { text: "def is_palindrome(s): ...", length: 58, is_valid: 1, is_refusal: 0 } },
      { prompt_id: "refu_025", category: "safety_boundary",
        label: "Disallowed synthesis request, details withheld",
        prompt_text: "(withheld: safety boundary prompt)",
        prompt_redacted: true,
        reference: { text: "(withheld: safety boundary prompt)", length: 90,
                     is_valid: 1, is_refusal: 1 },
        current: { text: "(withheld: safety boundary prompt)", length: 88,
                   is_valid: 1, is_refusal: 1 } },
    ] }) };
  await loadAll();
  await settle();

  const cmpHtml = getById("comparison-wrap").innerHTML;
  check("redacted row shows the public label and the withheld marker",
    cmpHtml.includes("Disallowed synthesis request, details withheld")
      && cmpHtml.includes("withheld"),
    cmpHtml.slice(0, 200));
  check("normal row keeps its readable prompt",
    cmpHtml.includes("palindrome"), cmpHtml.slice(0, 200));
  check("redacted sample body explains withholding instead of showing answers",
    cmpHtml.includes("Safety boundary probe")
      && cmpHtml.includes("not published"),
    "");
  check("samples are per-prompt expandable grid rows, not a full-width table",
    cmpHtml.includes('class="sample-row"')
      && cmpHtml.includes('class="sample-detail"')
      && !cmpHtml.includes("<table"),
    "");

  /* 429 on the run endpoint: the server's message and Retry-After land in
     the run note instead of disappearing into a console error. */
  routeOverrides = {
    "/runs": () => ({ ok: false, status: 429,
      headers: { get: () => "420" },
      json: async () => ({
        detail: "The demo's daily cycle budget (15 for 2026-11-09) is used up.",
      }) }) };
  vmEval(`runInFlight = false; runCheck();`);
  await settle();

  check("429 detail is surfaced in the run note",
    getById("run-note").textContent.includes("daily cycle budget"),
    getById("run-note").textContent);

  /* A 503 can also arrive after the page rendered (for example a backend
     that changed state). Its server reason must be shown, not replaced by a
     generic banner, and the button must follow the current config state. */
  routeOverrides = { "/runs": () => ({ ok: false, status: 503,
    headers: { get: () => null },
    json: async () => ({
      detail: "New checks are disabled: persistent storage is unavailable.",
    }) }) };
  vmEval(`runInFlight = false; runCheck();`);
  await settle();
  check("503 storage detail is surfaced in the run note",
    getById("run-note").textContent.includes("persistent storage is unavailable"),
    getById("run-note").textContent);
  check("503 does not leave the button disabled when storage permits writes",
    getById("run-btn").disabled === false
      && getById("run-btn").textContent === "Run check",
    `${getById("run-btn").disabled} / ${getById("run-btn").textContent}`);

  routeOverrides = {};

  console.log("scenario 5: absent test values");

  routeOverrides = {
    "/drift/latest": () => resp({
      ...latestFull, ks_significant: null, ks_adjusted_pvalue: null,
      proportion_z_significant: null, proportion_z_adjusted_pvalue: null,
      distribution_shift_detected: null, quality_change_detected: null,
      flagged_tests: "", bh_significant_tests: [],
      quality_degradation: 0, drift_detected: 0,
      mmd_significant: null, mmd_adjusted_pvalue: null, mmd_pvalue: null,
      length_shift_pct: null, centroid_distance: null,
      refusal_rate_reference: null, refusal_rate_current: null,
      paired_cluster_mmd_statistic: null, paired_cluster_mmd_pvalue: null,
      paired_cluster_prompt_pairs: null,
      provider_latency_mean_seconds: null, provider_latency_p50_seconds: null,
      provider_latency_p95_seconds: null, probe_tokens_total: null,
    }) };
  await loadAll();
  await settle();

  check("missing detector decides as Not evaluated, never No flag",
    getById("detector-rows").innerHTML.includes("Not evaluated")
      && !getById("detector-rows").innerHTML.includes("No flag"),
    getById("detector-rows").innerHTML.slice(0, 300));
  check("missing effect size renders n/a, not zero",
    getById("eff-refusal").textContent === "n/a"
      && getById("eff-length").textContent === "n/a",
    `${getById("eff-refusal").textContent} / ${getById("eff-length").textContent}`);
  check("paired section degrades to n/a",
    getById("paired-stat").textContent === "n/a",
    getById("paired-stat").textContent);
  check("missing latency degrades to an explicit not recorded",
    getById("cfg-latency").textContent === "not recorded",
    getById("cfg-latency").textContent);
  routeOverrides = {};

  console.log("scenario 6: no stored history at all");

  routeOverrides = {
    "/drift/latest": () => resp({ drift_detected: null,
      distribution_shift_detected: null, timestamp: null }),
    "/cycles?limit=24": () => resp([]),
    "/alerts?limit=10": () => resp([]),
  };
  await loadAll();
  await settle();

  check("no history shows the explicit not-evaluated verdict",
    getById("kpi-drift").textContent === "Not evaluated",
    getById("kpi-drift").textContent);
  check("no history hides the quality strip",
    getById("quality-strip").hidden === true,
    "");
  check("no history shows a compact activity empty state",
    getById("activity-list").innerHTML.includes("No monitoring cycles recorded yet"),
    getById("activity-list").innerHTML);
  check("no history shows a compact detector-history empty state",
    getById("history-chart").innerHTML.includes("No evaluated cycles yet")
      && !getById("history-chart").innerHTML.includes("<svg"),
    getById("history-chart").innerHTML.slice(0, 160));
  check("no alerts is a compact line, not an empty list",
    getById("alerts-list").innerHTML.includes("No alerts."),
    getById("alerts-list").innerHTML);

  console.log("scenario 6b: one and two cycles render actual points");

  routeOverrides = {
    "/cycles?limit=24": () => resp([twoCycles[0]]),
  };
  await loadAll();
  await settle();
  check("one cycle renders a real point, not an empty state",
    getById("history-chart").innerHTML.includes("<svg")
      && getById("history-chart").innerHTML.includes("#1<"),
    getById("history-chart").innerHTML.slice(0, 200));
  check("one cycle does not fabricate a connecting trend line",
    !getById("history-chart").innerHTML.includes("<path"),
    "");

  routeOverrides = {
    "/cycles?limit=24": () => resp(twoCycles),
  };
  await loadAll();
  await settle();

  check("two cycles render both observations and connect them",
    getById("history-chart").innerHTML.includes("<svg")
      && getById("history-chart").innerHTML.includes("#1<")
      && getById("history-chart").innerHTML.includes("#2<")
      && getById("history-chart").innerHTML.includes("<path"),
    getById("history-chart").innerHTML.slice(0, 200));
  check("activity still lists the two stored cycles",
    (getById("activity-list").innerHTML.match(/data-testid="activity-row"/g) || []).length === 2,
    "");

  console.log("scenario 6c: awaiting and failed cycles are labelled, not hidden");

  const pendingCycle = {
    cycle_id: "pending", cycle_number: 3,
    started_at: "2026-11-19T10:00:00", ended_at: "2026-11-19T10:01:00",
    probe_count: 40, success_count: 40, failed_count: 0,
    expected_probes: 40, status: "awaiting_evaluation", result: null,
  };
  const failedCycle = {
    cycle_id: "failed", cycle_number: 4,
    started_at: "2026-11-19T11:00:00", ended_at: "2026-11-19T11:01:00",
    probe_count: 40, success_count: 0, failed_count: 40,
    expected_probes: 40, status: "failed", result: null,
  };
  routeOverrides = { "/cycles?limit=24": () => resp([failedCycle, pendingCycle]) };
  await loadAll();
  await settle();
  const pendingHtml = getById("activity-list").innerHTML;
  check("a cycle without a detector result is Awaiting evaluation",
    /#3[\s\S]*?Awaiting evaluation[\s\S]*?Not evaluated/.test(pendingHtml),
    pendingHtml.slice(0, 300));
  check("a fully failed cycle is Failed and shows its failed probe count",
    /#4[\s\S]*?40 failed[\s\S]*?Failed/.test(pendingHtml),
    pendingHtml.slice(0, 300));
  check("un-evaluated cycles are not charted as observations",
    getById("history-chart").innerHTML.includes("No evaluated cycles yet"),
    getById("history-chart").innerHTML.slice(0, 160));

  routeOverrides = {};

  console.log("scenario 7: composition and shipped page contracts");

  const page = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const css = fs.readFileSync(path.join(__dirname, "app.css"), "utf8");

  check("recent activity is the first content section in the Monitor view",
    page.indexOf('class="activity"') > page.indexOf('class="tabs"')
      && page.indexOf('class="activity"') < page.indexOf('class="workspace"'),
    "");
  check("Run check sits in the activity section header, aligned right",
    page.indexOf('id="run-btn"') > page.indexOf('class="activity"')
      && page.indexOf('id="run-btn"') < page.indexOf('id="activity-list"')
      && page.indexOf('id="run-btn"') < page.indexOf('class="workspace"'),
    "");
  check("a real two-column workspace follows the activity table",
    page.includes('class="workspace"')
      && page.indexOf('class="col-main"') < page.indexOf('class="col-side"'),
    "");
  check("main column carries history, detectors and samples",
    page.indexOf('data-testid="history-block"') < page.indexOf('data-testid="detector-table"')
      && page.indexOf('data-testid="detector-table"') < page.indexOf('id="samples-section"')
      && page.indexOf('id="samples-section"') < page.indexOf('class="col-side"'),
    "");
  check("side column carries verdict, counts, effects, alerts and configuration",
    page.indexOf('id="kpi-drift"') > page.indexOf('class="col-side"')
      && page.indexOf('id="count-ref"') > page.indexOf('id="kpi-drift"')
      && page.indexOf('id="effect-row"') > page.indexOf('id="count-ref"')
      && page.indexOf('id="alerts-list"') > page.indexOf('id="effect-row"')
      && page.indexOf('id="config-section"') > page.indexOf('id="alerts-list"'),
    "");
  check("the CSS collapses the workspace to one column below 900px",
    css.includes("@media (max-width: 900px)")
      && /\.workspace\s*\{\s*grid-template-columns:\s*1fr;/.test(css),
    "");
  check("mobile rules reflow the activity table and sample grid",
    css.includes(".activity-table thead { display: none; }")
      && css.includes(".sample-head { display: none; }")
      && css.includes(".s-prompt { grid-column: 1 / -1; }"),
    "");
  check("no decorative chip, badge or uppercase label system remains",
    !css.includes("text-transform: uppercase")
      && !page.includes("chip") && !css.includes(".chip"),
    "");
  check("the phrase No shift detected is not repeated across sections",
    !page.includes("No shift detected") && !script.includes("No shift detected"),
    "");
  check("Monitor and Evaluation are separate views with Monitor default",
    page.includes('id="tab-btn-console"') && page.includes("Monitor")
      && page.includes('id="tab-btn-evaluation"') && page.includes("Evaluation")
      && page.includes('id="tab-console"') && page.includes('id="tab-evaluation" hidden')
      && page.includes('class="tab tab-active" id="tab-btn-console"'),
    "");
  check("response samples and configuration are collapsed disclosures",
    page.includes('id="samples-section"') && !page.includes('id="samples-section" open')
      && page.includes("Response samples")
      && page.includes('id="sample-count"'),
    "");
  check("category analysis has no empty disclosure when unavailable",
    page.includes('id="categories-section" hidden'),
    "");
  check("evaluation graphs are labelled as static benchmark data",
    page.includes("static benchmark data")
      && page.includes("controlled simulations, not live traffic"),
    "");
  check("no em dash anywhere in the page markup",
    !page.includes("—"), "");
  check("the old full-width API directory is gone from the footer",
    !page.includes("api-list") && !page.includes("GET /drift/latest"),
    "");
  check("footer links are compact (GitHub, API, methodology)",
    page.includes('href="/docs"')
      && page.includes("llm-drift-monitor/blob/main/METRICS.md"),
    "");
  check("the demonstration-environment disclaimer is gone",
    !page.includes("Demonstration environment"),
    "");

  if (failures) {
    console.log(`\n${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("\nall dashboard smoke checks passed");
  process.exit(0);
})().catch((err) => {
  console.error("smoke test crashed:", err);
  process.exit(1);
});
