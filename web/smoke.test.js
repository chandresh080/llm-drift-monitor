/* Dashboard smoke test, no dependencies, runs in plain Node.
   Loads web/app.js together with a minimal DOM stub and API fixtures, and
   asserts the integration contracts that Python tests cannot see.
   This is a JavaScript contract test, not a browser test: it verifies what
   the code does with the API responses, not rendering, timing or layout.

   Contract list:
   - each data endpoint renders the panel it feeds, independent of the
     other endpoints (a hanging /health in particular changes nothing)
   - the split signal families render separately: a response availability
     flag can never flip the distribution shift headline
   - redacted safety boundary prompts show their public label and a
     withheld chip, never raw wording
   - flagged counts read the field the API actually returns
   - missing test values render as an explicit state, never as "No flag"
   - product CSV string keys ("0.0") resolve against numeric lookups
   - the refusal rate change takes its sign from the delta itself
   - rate limit rejections surface the server's Retry-After message

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
   loops and a test must not keep the event loop alive. The real behavior of
   those loops is verified by the per endpoint scenarios below instead. */
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

function resp(json, opts = {}) {
  return { ok: opts.ok !== false, status: opts.status || 200,
           headers: { get: () => opts.retryAfter == null ? null : opts.retryAfter },
           json: async () => json };
}

function fixtureResponse(url) {
  const fn = routeOverrides[url] || getFixture(url);
  if (fn == null) throw new Error("app requested unplanned endpoint " + url);
  return fn();
}

const fixtures = {
  /* Readiness answers. The delay scenario overrides /health with a
     response that never resolves, which is strictly worse than a 15 s
     slow answer for this contract. */
  "/health": () => resp({ status: "ok", db: "ok", provider: "gemini-3.1-flash-lite",
                          provider_status: "ok" }),
  "/health/live": () => resp({ status: "ok" }),
  "/config": () => resp({
    target_model: "gemini-3.1-flash-lite", adapter_type: "GeminiAdapter",
    provider_available: true, provider_error: null,
    embedding_model: "all-MiniLM-L6-v2", reference_window: 120, current_window: 40,
    tests_start_at_responses: 160, prompt_panel_version: "3.0.0",
    prompt_panel_probes: 40, probes_per_cycle: 40, alpha: 0.05,
    min_consecutive_cycles: 2, category_window_cycles: 3,
    min_samples_per_category: 8,
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
  "/stats": () => resp({ probe_runs: 320, test_results: 12, alerts: 2 }),
  "/drift/latest": () => resp(latestFull),
  "/drift/results?limit=12": () => resp([]),
  "/alerts?limit=10": () => resp([]),
  "/drift/comparison": () => resp({
    ready: false, reference_stored: 120, reference_required: 120,
    current_stored: 40, current_required: 40, tests_start_at: 160, rows: [] }),
  "/drift/categories": () => resp({
    category_window_cycles: 3, min_samples_per_category: 8, rows: [] }),
  "/evaluation": () => resp(evaluation),
};

function getFixture(url) {
  return fixtures[url] || null;
}

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

  /* Readiness check ran via loadHealth and answered ok. */
  check("status pill shows service online",
    getById("health-pill").innerHTML.includes("Service online"),
    getById("health-pill").innerHTML);

  check("flagged count reads the canonical API field",
    getById("kpi-flagged").textContent.trim() === "1 / 3",
    getById("kpi-flagged").textContent);

  check("distribution headline follows the distribution family field",
    getById("kpi-drift").textContent === "Shift flagged",
    getById("kpi-drift").textContent);

  check("quality guardrail reported separately and not conflated with shift",
    getById("quality-strip").innerHTML.includes("Quality guardrail tripped"),
    getById("quality-strip").innerHTML.slice(0, 80));

  check("refusal rate change takes its sign from the delta, not the reference",
    getById("eff-refusal").textContent.startsWith("-5.0"),
    getById("eff-refusal").textContent);

  check("length effect size has an explicit plus for growth",
    getById("eff-length").textContent === "+19.5%",
    getById("eff-length").textContent);

  /* Empty-history comparison renders an explained bootstrap state. */
  check("fresh deployment state explains the two windows",
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

  check("config shows the run policy the throttle enforces",
    getById("cfg-runpolicy").textContent.includes("10 min")
      && getById("cfg-runpolicy").textContent.includes("15 checks")
      && getById("cfg-runpolicy").textContent.includes("4 used today"),
    getById("cfg-runpolicy").textContent);

  check("run note no longer hardcodes the old 10 response cycle",
    getById("run-note").textContent.includes("40 responses"),
    getById("run-note").textContent);

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

  const tradeoffHtml = getById("eval-tradeoff").innerHTML;
  check("persist2 marked shipped in the trade-off table",
    tradeoffHtml.includes("shipped"), "");

  /* p95 and the delay/false-alarm headline line stay faithful. */
  check("overhead renders with sample count",
    getById("ev-overhead").textContent === "388 ms"
      && getById("ev-overhead-sub").textContent.includes("12 recorded benchmark cycles"),
    getById("ev-overhead-sub").textContent);

  check("detection rate sub documents the limited trial basis",
    getById("ev-detect-sub").textContent.includes("limited"),
    getById("ev-detect-sub").textContent);

  console.log("scenario 3: a hanging /health must not stop any panel");

  /* Acceptance scenario from the review: /health never answers at all.
     Data endpoints answer and every dependent panel renders. */
  routeOverrides = { "/health": () => new Promise(() => {/* hangs forever */}) };
  vmEval("state.health = null; renderHealthPill();");
  await loadAll();
  await settle();

  check("stored responses still render with /health hanging",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("detectors still render with /health hanging",
    getById("det-mmd-decision").textContent === "Flagged",
    getById("det-mmd-decision").textContent);

  check("comparison still renders with /health hanging",
    getById("comparison-wrap").innerHTML.includes("Evaluation has not started yet"),
    "");

  check("pill says data is loaded while the status check is pending",
    getById("health-pill").innerHTML.includes("Data loaded"),
    getById("health-pill").innerHTML);

  routeOverrides = {};

  console.log("scenario 4: /health/live answers before readiness");

  /* New early state: process alive (fast endpoint), data still loading. */
  vmEval("loadLive(); state.awake = false; renderHealthPill();");
  await settle();

  check("pill shows the alive state before readiness answers",
    getById("health-pill").innerHTML.includes("Service alive"),
    getById("health-pill").innerHTML);

  console.log("scenario 5: split signals, redacted prompts, rate limits");

  /* availability fires, distribution does not: the headline must stay
     Stable and the quality line must name the real source. */
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
    getById("kpi-drift").textContent === "Stable",
    getById("kpi-drift").textContent);

  check("availability change is reported as its own quality signal",
    getById("quality-strip").innerHTML.includes("Response availability changed"),
    getById("quality-strip").innerHTML.slice(0, 120));

  routeOverrides = {};

  /* Redacted comparison: the safety boundary row shows only the public
     label plus the withheld marker. */
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
  check("redacted row shows the public label and the withheld chip",
    cmpHtml.includes("Disallowed synthesis request, details withheld")
      && cmpHtml.includes("withheld"),
    cmpHtml.slice(0, 200));
  check("normal row keeps its readable prompt",
    cmpHtml.includes("palindrome"), cmpHtml.slice(0, 200));
  routeOverrides = {};

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

  routeOverrides = {};

  console.log("scenario 6: absent test values");

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
    getById("det-ks-decision").textContent === "Not evaluated",
    getById("det-ks-decision").textContent);
  check("missing effect size renders not recorded",
    getById("eff-refusal").textContent === "not recorded",
    getById("eff-refusal").textContent);
  check("paired section degrades to not recorded",
    getById("paired-stat").textContent === "not recorded",
    getById("paired-stat").textContent);
  check("missing latency degrades to an explicit not recorded",
    getById("cfg-latency").textContent === "not recorded for the latest cycle",
    getById("cfg-latency").textContent);
  routeOverrides = {};

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
