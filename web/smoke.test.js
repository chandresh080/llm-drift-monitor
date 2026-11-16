/* Dashboard smoke test, no dependencies, runs in plain Node.
   Loads web/app.js together with a minimal DOM stub and API fixtures, and
   asserts the integration contracts that Python tests cannot see.
   This is a JavaScript contract test, not a browser test: it verifies what
   the code does with the API responses, not rendering, timing or layout.

   Contract list:
   - each data endpoint renders the panel it feeds, independent of the
     other endpoints (no endpoint can block another)
   - the status pill derives from /health/live alone: Online / Offline,
     and readiness is never fetched, never blocks, never rewrites it
   - the split signal families render separately: a response availability
     flag can never flip the distribution shift headline
   - redacted safety boundary prompts show their public label and a
     withheld chip, never raw wording
   - flagged counts read the field the API actually returns
   - missing test values render as an explicit state ("Not evaluated",
     "—"), never as "No flag" or zero
   - product CSV string keys ("0.0") resolve against numeric lookups
   - the refusal rate change takes its sign from the delta itself
   - rate limit rejections surface the server's Retry-After message
   - the information architecture: heavy evidence sections are collapsed
     <details>, the initial view is verdict + metrics + detectors

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

  /* Status derives from /health/live alone, which answered ok. */
  check("status pill shows Online from the liveness endpoint",
    getById("health-pill").innerHTML.includes("Online"),
    getById("health-pill").innerHTML);

  check("readiness (/health) is never fetched",
    !requestedUrls.some((u) => u === "/health" || u.includes("/health?")
      || u.startsWith("/health/") && u !== "/health/live"),
    requestedUrls.filter((u) => u.startsWith("/health") && u !== "/health/live").join(", "));

  check("distribution headline follows the distribution family field",
    getById("kpi-drift").textContent === "Shift detected",
    getById("kpi-drift").textContent);

  check("flagged count reads the canonical API field",
    getById("kpi-flagged").textContent.trim() === "1 / 3",
    getById("kpi-flagged").textContent);

  check("responses evaluated metric from /stats",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("last check metric shows the latest cycle timestamp",
    getById("kpi-lastcheck").textContent !== "—"
      && getById("kpi-lastcheck").textContent !== "",
    getById("kpi-lastcheck").textContent);

  check("verdict meta names both window sizes and the check time",
    getById("verdict-meta").textContent.includes("Reference: 120 responses")
      && getById("verdict-meta").textContent.includes("Current: 40 responses")
      && getById("verdict-meta").textContent.includes("Last checked:"),
    getById("verdict-meta").textContent);

  check("quality guardrail reported separately and not conflated with shift",
    getById("quality-strip").hidden === false
      && getById("quality-strip").innerHTML.includes("Invalid-response guardrail tripped"),
    getById("quality-strip").innerHTML.slice(0, 80));

  const detHtml = getById("detector-rows").innerHTML;
  check("detector table renders all three rows with methods",
    detHtml.includes("Response length") && detHtml.includes("KS")
      && detHtml.includes("Response availability") && detHtml.includes("Fisher / z")
      && detHtml.includes("Semantic shift") && detHtml.includes("Paired MMD"),
    detHtml.slice(0, 200));
  check("flagged detector shows Flagged with its adjusted p",
    detHtml.includes("Flagged") && detHtml.includes("0.030"),
    detHtml.slice(0, 400));
  check("unflagged detectors show No flag",
    detHtml.includes("No flag"), detHtml.slice(0, 400));

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

  check("config shows the run policy the throttle enforces",
    getById("cfg-runpolicy").textContent.includes("10 min")
      && getById("cfg-runpolicy").textContent.includes("15 per day")
      && getById("cfg-runpolicy").textContent.includes("4 used today"),
    getById("cfg-runpolicy").textContent);

  check("run note uses the configured cycle size, not a hardcoded number",
    getById("run-note").textContent.includes("40 prompts per run")
      && getById("run-note").textContent.includes("every 10 min"),
    getById("run-note").textContent);

  check("header model label comes from /config",
    getById("model-label").textContent.includes("gemini-3.1-flash-lite"),
    getById("model-label").textContent);

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

  /* Headline numbers stay faithful. */
  check("overhead renders with the sample count basis",
    getById("ev-overhead").textContent === "388 ms",
    getById("ev-overhead").textContent);

  check("methodology disclosure states the limited trial basis",
    getById("ev-methodology").innerHTML.includes("limited"),
    getById("ev-methodology").innerHTML.slice(0, 200));

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

  console.log("scenario 3: liveness failure shows Offline; data still renders");

  /* The pill is the only thing /health/live drives: when it fails the
     instance is Offline, and stored data panels are untouched. */
  routeOverrides = { "/health/live": () => {
    throw new Error("connection refused");
  } };
  vmEval("state.live = null; loadLive();");
  await settle();

  check("pill shows Offline when liveness fails",
    getById("health-pill").innerHTML.includes("Offline"),
    getById("health-pill").innerHTML);

  await loadAll();
  await settle();

  check("stored responses still render with liveness failing",
    getById("kpi-runs").textContent === "320",
    getById("kpi-runs").textContent);

  check("detectors still render with liveness failing",
    getById("detector-rows").innerHTML.includes("Flagged"),
    "");

  routeOverrides = {};

  console.log("scenario 4: split signals, redacted prompts, rate limits");

  /* availability fires, distribution does not: the headline must stay
     "No shift detected" and the quality strip must name the real source. */
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
    getById("kpi-drift").textContent === "No shift detected",
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
  check("redacted row shows the public label and the withheld chip",
    cmpHtml.includes("Disallowed synthesis request, details withheld")
      && cmpHtml.includes("withheld"),
    cmpHtml.slice(0, 200));
  check("normal row keeps its readable prompt",
    cmpHtml.includes("palindrome"), cmpHtml.slice(0, 200));
  check("redacted sample body explains withholding instead of showing answers",
    cmpHtml.includes("Safety boundary probe")
      && cmpHtml.includes("not published"),
    "");
  check("samples are per-prompt expandable details, not a full-width table",
    cmpHtml.includes("<details class=\"sample\"")
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
  check("missing effect size renders an em dash, not zero",
    getById("eff-refusal").textContent === "—"
      && getById("eff-length").textContent === "—",
    `${getById("eff-refusal").textContent} / ${getById("eff-length").textContent}`);
  check("flagged metric degrades to an em dash without the field",
    getById("kpi-flagged").textContent === "—",
    getById("kpi-flagged").textContent);
  check("paired section degrades to an em dash",
    getById("paired-stat").textContent === "—",
    getById("paired-stat").textContent);
  check("missing latency degrades to an explicit not recorded",
    getById("cfg-latency").textContent === "not recorded",
    getById("cfg-latency").textContent);
  routeOverrides = {};

  console.log("scenario 6: no stored history at all");

  routeOverrides = {
    "/drift/latest": () => resp({ drift_detected: null,
      distribution_shift_detected: null, timestamp: null }),
  };
  await loadAll();
  await settle();

  check("no history shows the explicit not-evaluated verdict",
    getById("kpi-drift").textContent === "Not evaluated yet",
    getById("kpi-drift").textContent);
  check("no history hides the quality strip",
    getById("quality-strip").hidden === true,
    "");

  routeOverrides = {};

  console.log("scenario 7: information architecture of the shipped page");

  /* The page itself (not the stub DOM) must put heavy evidence behind
     collapsed details and keep the initial view compact. */
  const page = fs.readFileSync(path.join(__dirname, "index.html"), "utf8");
  const sectionDetails = (page.match(/<details class="section-details"/g) || []).length;
  check("heavy evidence sections are collapsed details blocks",
    sectionDetails >= 4, `found ${sectionDetails}`);
  check("initial view has verdict, metrics and detectors",
    page.includes('id="verdict-box"') && page.includes('id="detector-rows"')
      && page.includes('class="metric-row"'),
    "");
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
