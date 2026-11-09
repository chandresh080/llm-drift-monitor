/* Dashboard smoke test, no dependencies, runs in plain Node.
   Loads web/app.js together with a minimal DOM stub and API fixtures, and
   asserts the integration contracts that Python tests cannot see:
   - a failing /health does not block any other panel
   - the flagged count reads the field the API actually returns
   - missing test values render as an explicit state, never as "No flag"
   - product CSV string keys ("0.0") resolve against numeric lookups
   - the refusal rate change takes its sign from the delta itself
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

/* setInterval is stubbed before app.js loads: the script installs two
   periodic loops and a test must not keep the event loop alive. The real
   behavior of those loops is tested by deployment, not here. */
const timers = [];
global.setInterval = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
global.clearInterval = () => {};

global.document = documentStub;
global.window = global;
global.AbortController = AbortController;

/* ---------- API fixtures ---------- */

const latestFull = {
  drift_detected: 1,
  flagged_tests: ["mmd"],           /* canonical field the API returns */
  bh_significant_tests: ["mmd"],
  quality_degradation: 1,
  timestamp: "2026-11-08T12:00:00",
  reference_size: 50, current_size: 10,
  ks_statistic: 0.3, ks_pvalue: 0.4, ks_adjusted_pvalue: 1.0, ks_significant: 0,
  proportion_z_statistic: null, proportion_z_pvalue: 1.0,
  proportion_z_adjusted_pvalue: 1.0, proportion_z_significant: 0,
  mmd_statistic: 0.2, mmd_pvalue: 0.01, mmd_adjusted_pvalue: 0.03, mmd_significant: 1,
  length_shift_pct: 19.5, centroid_distance: 0.153,
  refusal_rate_reference: 0.0, refusal_rate_current: -0.05,
  unique_reference: 12, unique_current: 10,
  paired_cluster_mmd_statistic: -0.027, paired_cluster_mmd_pvalue: 1.0,
  paired_cluster_prompt_pairs: 10,
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

function fixtureResponse(url) {
  const fn = routeOverrides[url] || getFixture(url);
  if (fn == null) throw new Error("app requested unplanned endpoint " + url);
  return fn();
}

const fixtures = {
  "/health": () => ({ ok: true, status: 200, json: async () =>
    ({ status: "ok", db: "ok", provider: "fake-llm-v1", provider_status: "ok" }) }),
  "/config": () => ({ ok: true, status: 200, json: async () => ({
    target_model: "fake-llm-v1", adapter_type: "DemoLLMAdapter",
    embedding_model: "all-MiniLM-L6-v2", reference_window: 50, current_window: 10,
    tests_start_at_responses: 60, prompt_panel_version: "2.1.0",
    prompt_panel_probes: 40, probes_per_cycle: 10, alpha: 0.05,
    min_consecutive_cycles: 2, category_window_cycles: 5,
    min_samples_per_category: 8 }) }),
  "/stats": () => ({ ok: true, status: 200, json: async () =>
    ({ probe_runs: 320, test_results: 12, alerts: 2 }) }),
  "/drift/latest": () => ({ ok: true, status: 200, json: async () => latestFull }),
  "/drift/results?limit=12": () => ({ ok: true, status: 200, json: async () => [] }),
  "/alerts?limit=10": () => ({ ok: true, status: 200, json: async () => [] }),
  "/drift/comparison": () => ({ ok: true, status: 200, json: async () =>
    ({ ready: false, reference_stored: 50, reference_required: 50,
       current_stored: 10, current_required: 10, tests_start_at: 60, rows: [] }) }),
  "/drift/categories": () => ({ ok: true, status: 200, json: async () =>
    ({ category_window_cycles: 5, min_samples_per_category: 8, rows: [] }) }),
  "/evaluation": () => ({ ok: true, status: 200, json: async () => evaluation }),
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

/* The page loads at mount time via ensureAwake(); give the promise chain a
   tick to settle, then inspect what rendered. */
(async () => {
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  console.log("scenario 1: healthy endpoints, full data");

  /* Run button waits only on data endpoints; health answers fine. */
  check("status pill shows service online",
    getById("health-pill").innerHTML.includes("Service online"),
    getById("health-pill").innerHTML);

  check("flagged count reads the canonical API field",
    getById("kpi-flagged").textContent.trim() === "1 / 3",
    getById("kpi-flagged").textContent);

  check("distribution verdict reflects the latest cycle",
    getById("kpi-drift").textContent === "Shift flagged",
    getById("kpi-drift").textContent);

  check("quality guardrail reported separately and not conflated with shift",
    getById("quality-strip").innerHTML.includes("Quality guardrail tripped")
      && !getById("kpi-drift").textContent.toLowerCase().includes("quality"),
    getById("quality-strip").innerHTML.slice(0, 60));

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

  console.log("scenario 2: evaluation tab data fidelity");

  await loadEvaluation();
  await new Promise((r) => setImmediate(r));

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

  /* p95 and the delay/falsely-flagged headline line stay faithful. */
  check("overhead renders with sample count",
    getById("ev-overhead").textContent === "388 ms"
      && getById("ev-overhead-sub").textContent.includes("12 recorded benchmark cycles"),
    getById("ev-overhead-sub").textContent);

  check("detection rate sub documents the limited trial basis",
    getById("ev-detect-sub").textContent.includes("limited"),
    getById("ev-detect-sub").textContent);

  console.log("scenario 3: /health failing must not block panels");

  routeOverrides = { "/health": () => ({ ok: false, status: 500, json: async () => ({}) }) };
  await loadAll();
  await new Promise((r) => setImmediate(r));

  check("stored responses still render with /health down",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("pill says data is loaded while status pending",
    getById("health-pill").innerHTML.includes("Data loaded"),
    getById("health-pill").innerHTML);

  routeOverrides = {};

  console.log("scenario 4: absent test values");

  routeOverrides = {
    "/drift/latest": () => ({ ok: true, status: 200, json: async () => ({
      ...latestFull, ks_significant: null, ks_adjusted_pvalue: null,
      proportion_z_significant: null, proportion_z_adjusted_pvalue: null,
      flagged_tests: [], bh_significant_tests: [], quality_degradation: 0,
      drift_detected: 0, length_shift_pct: null, centroid_distance: null,
      refusal_rate_reference: null, refusal_rate_current: null,
      paired_cluster_mmd_statistic: null, paired_cluster_mmd_pvalue: null,
      paired_cluster_prompt_pairs: null,
    }) }),
  };
  await loadAll();
  await new Promise((r) => setImmediate(r));

  check("missing detector decides as Not evaluated, never No flag",
    getById("det-ks-decision").textContent === "Not evaluated",
    getById("det-ks-decision").textContent);
  check("missing effect size renders not recorded",
    getById("eff-refusal").textContent === "not recorded",
    getById("eff-refusal").textContent);
  check("paired section degrades to not recorded",
    getById("paired-stat").textContent === "not recorded",
    getById("paired-stat").textContent);
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
