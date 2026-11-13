/* Drift Monitor live console.
   Vanilla JS, no dependencies; all numbers come from the REST API on the
   same origin. Nothing is computed client side except formatting.

   Loading contract: every data endpoint is fetched and rendered
   independently, so each panel appears at the earliest moment its own
   data is available. /health (readiness, can be slow) and /health/live
   (liveness, fast) are separate tasks: they update the status pill only
   and can never block a panel. */

const state = {
  health: null,       // /health payload (readiness; may be slow or absent)
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
  failed: new Set(),  // data endpoints that did not answer (explicit unavailable states)
  awake: false,       // at least one data endpoint has answered
  wakeStartedAt: null,
};

const WAKE_WAIT_MS = 5000;
const REQUEST_TIMEOUT_MS = 15000;
const NUM_TESTS = 3;      // pooled family: KS, availability, MMD
const ALPHA = 0.05;       // only for display fallback; the API reports decisions
const PUBLIC_RUN_INTERVAL_SECONDS = 600;  // mirror of the API run policy
const DAILY_CYCLE_BUDGET = 15;

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
  pill.setAttribute("data-status", text.split(" ")[0]);
  pill.innerHTML = `<span class="dot"></span>${escapeHtml(text)}`;
}

/* The pill reflects three independent things, in priority order: whether
   stored data could be read, whether the process is alive at all, and
   what the (possibly slow) readiness check reports. Data availability
   wins the slot; readiness only downgrades the wording. */
function renderHealthPill() {
  if (!state.awake) {
    if (state.live) {
      setPill("pill-wake", "Service alive · loading data");
    } else {
      setPill("pill-wake",
        `connecting · ${fmtWait(Date.now() - (state.wakeStartedAt || Date.now()))}`);
    }
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
    const bits = [`provider ${state.health.provider_status}`
      + `(model ${state.health.provider || "unknown"})`,
      `database ${state.health.db}`];
    if (state.health.provider_error) {
      bits.push(state.health.provider_error);
    }
    banner("The service is reachable but a dependency is not ready: "
      + bits.join("; ") + ". Stored history stays visible either way; new "
      + "checks wait until the dependency is back.");
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

function latestSignals(l) {
  /* The split families. Legacy fallback for stored history recorded
     before the split fields existed: derive from the per test flags. */
  const dist = l.distribution_shift_detected != null
    ? Number(l.distribution_shift_detected)
    : (Number(l.ks_significant) === 1 || Number(l.mmd_significant) === 1 ? 1 : 0);
  const qualityChange = l.quality_change_detected != null
    ? Number(l.quality_change_detected)
    : Number(l.proportion_z_significant) === 1;
  const guardrail = Number(l.quality_degradation) === 1;
  return { dist, qualityChange: Boolean(qualityChange), guardrail };
}

function renderKpis() {
  const latest = state.latest;
  const driftEl = $("kpi-drift"), driftSub = $("kpi-drift-sub");
  const qualityStrip = $("quality-strip");
  const evaluated = latest && (latest.drift_detected != null
    || latest.distribution_shift_detected != null);
  if (!evaluated) {
    driftEl.textContent = "No evaluation yet";
    driftEl.className = "kpi-value";
    driftSub.textContent = "statistical evaluation starts once a full reference window and one current cycle are stored";
    qualityStrip.hidden = true;
  } else {
    const sig = latestSignals(latest);
    if (sig.dist === 1) {
      driftEl.textContent = "Shift flagged";
      driftEl.className = "kpi-value kpi-alert";
    } else {
      driftEl.textContent = "Stable";
      driftEl.className = "kpi-value kpi-ok";
    }
    driftSub.textContent =
      `distribution shift status for the latest cycle (${fmtTime(latest.timestamp)}) vs the reference window`;
    driftSub.title = detectedWithoutQualityCopy(sig.dist === 1);
    /* Separate quality status, two distinct sources: the statistical
       comparison of the non empty response rate between windows, and the
       absolute invalid share guardrail on the current window. Neither is a
       distribution verdict and neither can drive the headline above. */
    qualityStrip.hidden = false;
    const parts = [];
    if (sig.qualityChange) {
      parts.push('<span class="chip chip-alert">Response availability changed</span>'
        + '<span class="muted">the non empty response rate differs between the windows '
        + 'after Benjamini-Hochberg correction (statistical comparison).</span>');
    }
    if (sig.guardrail) {
      parts.push('<span class="chip chip-alert">Quality guardrail tripped</span>'
        + '<span class="muted">the invalid response share reached the configured '
        + 'threshold in the current window (absolute guardrail, not a statistical '
        + 'comparison).</span>');
    }
    if (!parts.length) {
      parts.push('<span class="chip chip-ok">Quality: no change flagged</span>'
        + '<span class="muted">non empty response rate comparable between the windows, '
        + 'invalid share under the absolute threshold.</span>');
    }
    qualityStrip.innerHTML = parts.join("");
    if (sig.qualityChange || sig.guardrail) {
      banner("Quality signal flagged (response availability or the invalid share "
        + "guardrail), not a distribution verdict. The distribution shift status above "
        + "follows the distributional detectors only.");
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
    runsEl.textContent = state.failed.has("stats") ? "unavailable" : "...";
    runsEl.className = "kpi-value"
      + (state.failed.has("stats") ? " effect-missing" : "");
  }

  /* Monitoring backend label describes what generated the stored answers:
     the config names it; the readiness check only corroborates it later. */
  const backendEl = $("kpi-provider");
  if (state.config && state.config.target_model) {
    backendEl.textContent = state.config.provider_available === false
      ? `${state.config.target_model} (unavailable)`
      : state.config.target_model;
    backendEl.className = "kpi-value"
      + (state.config.provider_available === false ? " effect-missing" : "");
  } else if (state.health && state.health.provider) {
    backendEl.textContent = state.health.provider;
    backendEl.className = "kpi-value";
  } else {
    backendEl.textContent = state.failed.has("config") ? "unavailable" : "...";
    backendEl.className = "kpi-value"
      + (state.failed.has("config") ? " effect-missing" : "");
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
    const panel = $("cfg-model").closest(".config-details");
    if (panel) {
      panel.classList.add("effect-missing");
      $("cfg-model").textContent = state.failed.has("config")
        ? "endpoint unavailable" : "...";
    }
    return;
  }
  // Provider identity is explicit: the real model name, and when the
  // deployment lacks its provider key the unavailable state is on the
  // surface, never a synthetic backend answer hiding behind it.
  $("cfg-model").textContent = c.provider_available === false
    ? `${c.target_model} (unavailable to run new cycles)`
    : `${c.target_model} (${c.adapter_type})`;
  $("cfg-embedding").textContent = c.embedding_model;
  const cyclesForWindow = c.probes_per_cycle
    ? Math.round(c.reference_window / c.probes_per_cycle) : null;
  $("cfg-ref").textContent = cyclesForWindow
    ? `${c.reference_window} responses (${cyclesForWindow} complete cycles)`
    : `${c.reference_window} responses`;
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

  /* Provider cost visibility, latest cycle: per call latency percentiles
     and provider reported tokens when the adapter exposes them. Nothing
     here is a quality judgement. */
  const l = state.latest || {};
  $("cfg-latency").textContent =
    l.provider_latency_mean_seconds == null
      ? "not recorded for the latest cycle"
      : `${Number(l.provider_latency_mean_seconds).toFixed(1)} s mean, `
        + `${Number(l.provider_latency_p50_seconds).toFixed(1)} s p50, `
        + `${Number(l.provider_latency_p95_seconds).toFixed(1)} s p95 per provider call`;
  $("cfg-tokens").textContent =
    l.probe_tokens_total == null
      ? "not recorded for the latest cycle"
      : `${Number(l.probe_tokens_total).toLocaleString()} tokens across the cycle (provider reported)`;

  /* The manifest is the provenance of the reference window: what really
     produced the data every comparison stands on. */
  const m = c.baseline_manifest;
  $("cfg-manifest").textContent = m
    ? `${m.provider} ${m.model_id}, panel v${m.panel_version}, `
      + `temperature ${m.temperature}, collected ${fmtTime(m.collected_at)}`
    : "not recorded for this database";

  const rp = c.run_policy || {};
  const used = rp.daily_cycles_used_today != null
    ? Number(rp.daily_cycles_used_today) : null;
  $("cfg-runpolicy").textContent =
    `one check per visitor every ${Math.round((rp.per_visitor_interval_seconds
      || PUBLIC_RUN_INTERVAL_SECONDS) / 60)} min, shared daily budget of `
    + `${rp.daily_cycle_budget != null ? rp.daily_cycle_budget : DAILY_CYCLE_BUDGET} checks`
    + (used != null ? ` (${used} used today)` : "");
  renderRunNoteBaseline();
}

/* Match the static run note to the config once it is known, instead of a
   hardcoded 10/60 that other deployments contradict. */
function renderRunNoteBaseline() {
  const c = state.config;
  if (!c || $("run-note").dataset.active) return;
  $("run-note").textContent =
    `Each run records ${c.current_window} responses through the target model, so `
    + `checks are rate limited per visitor. Statistical evaluation starts once `
    + `${c.tests_start_at_responses} responses are stored.`;
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
    // Accurate naming: is_valid is exactly the non-empty check, so the
    // chips say that and nothing stronger.
    chips.push('<span class="meta-chip ' + (data.is_valid ? "chip-ok-line" : "chip-alert-line") + '">'
      + (data.is_valid ? "non-empty" : "empty") + "</span>");
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
    wrap.innerHTML = '<p class="muted">'
      + (state.failed.has("comparison")
        ? "Comparison endpoint unavailable (it did not answer)."
        : "Comparison panel unavailable in this deployment.")
      + "</p>";
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
        ${c.current_required} current responses. Once ${c.tests_start_at}
        responses are stored, each cycle runs all three tests and this panel shows the
        answers side by side. This is expected behaviour until then, not a fault.
      </div>`;
    return;
  }
  const rows = c.rows.map((r, i) => {
    const labelHtml = `<strong>${escapeHtml(r.label || r.prompt_text)}</strong>`
      + (r.prompt_redacted
        ? ' <span class="meta-chip chip-alert-line">prompt withheld</span>' : "");
    return `
    <tr class="cmp-row" data-i="${i}" data-env="${r.prompt_redacted ? "1" : "0"}">
      <td class="cmp-cat"><span class="chip chip-cat">${escapeHtml(r.category)}</span></td>
      <td class="cmp-prompt">${labelHtml}<div class="panel-note"><code>${escapeHtml(r.prompt_text)}</code></div></td>
      <td class="cmp-resp">${responseCell(r.reference)}</td>
      <td class="cmp-resp">${responseCell(r.current)}</td>
    </tr>`;
  }).join("");
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
      newest answer from the latest cycle. Prompts in the safety boundary group appear as
      public labels only, and their exact wording and answers are withheld.</p>`;

  const detail = wrap.querySelector("#cmp-detail");
  wrap.querySelectorAll(".cmp-row").forEach((tr) => {
    tr.addEventListener("click", () => {
      const r = c.rows[Number(tr.dataset.i)];
      if (r.prompt_redacted) {
        detail.innerHTML = `
          <details class="cmp-detail">
            <summary>${escapeHtml(r.label || r.prompt_text)}
              <span class="chip chip-cat">${escapeHtml(r.category)}</span>
              <span class="chip chip-warn">withheld</span></summary>
            <div class="alert-body">
              <p class="muted">This probe belongs to the safety boundary group: the exact
                request and the model's answers are not published on this page. The
                metadata (lengths, validity and refusal flags) remains visible in the
                table.</p>
            </div>
          </details>`;
        return;
      }
      detail.innerHTML = `
        <details open class="cmp-detail">
          <summary>${escapeHtml(r.label || r.prompt_text)} <span class="chip chip-cat">${escapeHtml(r.category)}</span></summary>
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
  if (state.failed.has("results")) {
    el.innerHTML = '<p class="muted">Recent cycles unavailable (the endpoint did not answer). It is retried on the next refresh.</p>';
    return;
  }
  const rows = (state.results || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No completed cycles stored yet.</p>';
    return;
  }
  el.innerHTML = rows.map((r) => {
    const ref = Number(r.reference_size), cur = Number(r.current_size);
    const insufficient = !r.reference_size || !r.current_size;
    // Split families. History rows recorded before the split still read
    // correctly: the distribution verdict falls back to the per test
    // distributional flags, never to the validity test.
    const distShifted = Number(r.ks_significant) === 1
      || Number(r.mmd_significant) === 1;
    const qualityFlagged = Number(r.proportion_z_significant) === 1
      || Number(r.overall_quality_degradation) === 1;
    let fired = [];
    const raw = r.bh_significant_tests;
    try { fired = typeof raw === "string" ? JSON.parse(raw) : (raw || []); } catch (e) { fired = []; }
    return `
      <div class="act-row">
        <span class="chip ${insufficient ? "chip-warn" : (distShifted ? "chip-alert" : "chip-ok")}">${insufficient ? "Insufficient data" : (distShifted ? "Distribution shift flagged" : "No distribution shift")}${!insufficient && fired.length ? " (" + fired.join(", ") + ")" : ""}</span>
        ${!insufficient && qualityFlagged ? '<span class="chip chip-alert">quality signal flagged</span>' : ""}
        <span class="muted">cycle ${r.id} &middot; ${fmtTime(r.timestamp)} &middot; ${cur} vs ${ref} responses</span>
      </div>`;
  }).join("");
}

function renderAlerts() {
  const el = $("alerts-list");
  if (state.failed.has("alerts")) {
    el.innerHTML = '<p class="muted">Alerts unavailable (the endpoint did not answer). It is retried on the next refresh.</p>';
    return;
  }
  const rows = (state.alerts || []).slice(0, 8);
  if (!rows.length) {
    el.innerHTML = '<p class="muted">No alerts raised yet. An alert requires the same '
      + 'verdict on consecutive cycles; a single flagged cycle is recorded but stays silent.</p>';
    return;
  }
  el.innerHTML = rows.map((a) => {
    const type = a.alert_type === "drift" ? "Distribution shift alert" : "Quality signal alert";
    const parts = [];
    parts.push(a.alert_type === "drift"
      ? "The distributional detectors flagged on consecutive cycles"
      : "A quality signal (response availability change or the invalid share "
        + "guardrail) fired on consecutive cycles");
    if (a.mmd_pvalue != null) parts.push(`semantic p ${fmtP(a.mmd_pvalue)}`);
    if (a.ks_pvalue != null) parts.push(`length p ${fmtP(a.ks_pvalue)}`);
    if (a.proportion_z_pvalue != null) parts.push(`availability p ${fmtP(a.proportion_z_pvalue)}`);
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

/* Each data endpoint is its own task: fetch, store, and render exactly the
   panels that depend on it, then move on. No endpoint waits for another,
   and nothing here touches /health at all; that lives in loadHealth below.
   A failed endpoint records itself in state.failed so its panel renders an
   explicit unavailable state instead of a silently misleading empty one. */
const DATA_LOADERS = [
  {
    key: "config", url: "/config",
    render: () => { renderConfig(); renderKpis(); },
  },
  {
    key: "stats", url: "/stats",
    render: renderKpis,
  },
  {
    key: "latest", url: "/drift/latest",
    render: () => {
      renderKpis();
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
    renderHealthPill();
  }
}

/* Kick off every data task; each panel renders inside its own task without
   waiting for the others. loadAll only aggregates the promises for callers
   that want to know when everything has settled. */
function loadAll() {
  return Promise.allSettled(DATA_LOADERS.map((def) => loadOne(def)));
}

/* Readiness (/health, can take seconds) runs completely independently of
   the data tasks: it never renders a panel, only the pill and banner. */
async function loadHealth() {
  try {
    state.health = await fetchJson("/health");
  } catch (err) {
    state.health = null;
  }
  renderHealthPill();
}

/* Fast liveness probe for an early pill state, separate from readiness. */
async function loadLive() {
  try {
    state.live = await fetchJson("/health/live");
  } catch (err) {
    state.live = null;
  }
  renderHealthPill();
}

function ensureAwake() {
  if (state.awake) return Promise.resolve(true);
  if (wakePromise) return wakePromise;
  wakeStarted();
  wakePromise = (async () => {
    const startedAt = state.wakeStartedAt;
    let explained = false;
    for (;;) {
      /* Fire every data task concurrently; Promise.any resolves as soon as
         ONE answers, no matter how slow the other endpoints (or /health)
         are, so the next retry round is not held back either. */
      const tasks = DATA_LOADERS.map((def) => loadOne(def));
      await Promise.race([
        Promise.any(tasks).catch(() => false),
        new Promise((r) => setTimeout(r, 8000)),
      ]);
      if (state.awake) {
        loadHealth();  // readiness off the critical path entirely
        banner("Connected. Panels render from the stored monitoring history; the status "
          + "pill confirms dependency readiness separately.");
        return true;
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
  loadProviderComparison();
  try {
    state.evaluation = await fetchJson("/evaluation");
  } catch (err) {
    $("evaluation-body").innerHTML =
      '<p class="muted">Evaluation results unavailable in this deployment.</p>';
    return;
  }
  renderEvaluation();
}

/* The recorded Groq versus Gemini comparison lives in a committed static
   artifact built by scripts/export_provider_comparison.py from the stored
   experiment outputs. Loading it is its own task (a static file, no API
   work) so the evaluation tab never waits on it. */
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
    box.innerHTML =
      '<p class="muted">Recorded comparison unavailable in this deployment.</p>';
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
    return {
      p50: range(p50, 2), p95: range(p95, 2),
      tokens: range(tokens, 0), ok, failed,
    };
  };
  const ref = agg(pc.reference.cycles);
  const cur = agg(pc.current.cycles);
  const validity = (rows) => {
    const vals = rows.map((r) => r.validity_rate).filter((v) => v != null);
    if (!vals.length) return "not measured (tests ran on the current stage only)";
    const min = Math.min(...vals) * 100, max = Math.max(...vals) * 100;
    return min === max ? `${min.toFixed(0)}%` : `${min.toFixed(0)} to ${max.toFixed(0)}%`;
  };
  const row = (label, stage, model, a, v) => `
    <tr><td>${label}</td><td>${stage}</td><td>${model}</td>
    <td>${a.ok} / ${a.failed}</td><td>${a.p50 || "n/a"}</td>
    <td>${a.p95 || "n/a"}</td><td>${a.tokens || "n/a"}</td><td>${v}</td></tr>`;
  box.innerHTML = `
    <table class="plain-table"><thead><tr>
      <th>Provider</th><th>Stage</th><th>Model</th><th>Stored answers (ok / failed)</th>
      <th>Latency p50 (s)</th><th>Latency p95 (s)</th><th>Tokens per cycle</th>
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
      `Detector verdicts: ${noFlag
        ? "no corrected test flagged in either the single cycle comparison or the five cycle extension, and no alert fired"
        : "one or more tests flagged (see the committed artifacts)"}. `
      + `The paired prompt level permutation reported raw p ${fmtP(f1.paired_mmd_pvalue)} `
      + `over ${f1.paired_prompt_pairs} matched prompts. Shared settings: prompt panel `
      + `version ${pc.shared.prompt_panel_version}, ${pc.shared.prompts_per_cycle} prompts per `
      + `cycle, temperature ${pc.shared.temperature}, max_tokens ${pc.shared.max_tokens}, `
      + `embeddings ${pc.shared.embedding_model}. Declared uncontrolled differences: `
      + `${pc.shared.uncontrolled_differences.map((d) => String(d)).join("; ")}. `
      + `Sources: ${pc.source_artifacts.join(", ")}, derived by `
      + `${pc.generated_by}. ${pc.caveat}`;
  }
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
      if (err.status === 429) {
        $("run-note").dataset.active = "1";
        let when = "";
        const waitS = err.retryAfter ? parseInt(err.retryAfter, 10) : NaN;
        if (!isNaN(waitS) && waitS < 86400) {
          when = " A run can be started at about "
            + new Date(Date.now() + waitS * 1000).toLocaleTimeString(
              undefined, { hour: "2-digit", minute: "2-digit" }) + ".";
        }
        note.textContent = (err.detail
          ? err.detail
          : "A rate limit was reached (per visitor interval or the daily cycle budget).")
          + when;
        return;
      }
      throw err;
    }
    const runId = started.run_id;
    const cfg = state.config || {};
    $("run-note").dataset.active = "1";
    note.textContent = `Running: ${cfg.probes_per_cycle || 10} responses are collected `
      + `and evaluated, which can take several minutes on the hosted tier.`;

    /* Hosted free tier cycles can take minutes when provider call starts
       are spread to respect quota; poll patiently with generous ticks. The
       instance can also recycle mid check (free tier pause), in which case
       the run record disappears: consecutive misses stop the loop with an
       honest message instead of polling a ghost forever. */
    let misses = 0;
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 10000));
      const st = await fetchJson("/runs/" + runId).catch(() => null);
      if (!st) {
        misses += 1;
        if (misses >= 3) {
          note.textContent = "The hosted instance recycled while this check "
            + "ran, so its record is gone and nothing was committed. "
            + "Panels show the last stored cycle; run the check again.";
          await loadAll();
          return;
        }
        continue;
      }
      misses = 0;

      if (st.status === "lost") {
        note.textContent = st.error
          || "The hosted instance recycled while this check ran; run again.";
        await loadAll();
        return;
      }

      if (st.status === "completed") {
        const r = st.result || {};
        if (r.tests_run) {
          await loadAll();
          loadHealth();
          const latest = state.latest || {};
          const sig = latestSignals(latest);
          note.textContent = sig.dist === 1
            ? "Distribution shift flagged in this cycle. The detector cards and alert "
              + "panel show the evidence; an alert fires only if this repeats next cycle."
            : "Cycle complete. No statistically significant distributional difference "
              + "from the reference window.";
          const fired = Array.isArray(latest.flagged_tests) ? latest.flagged_tests : [];
          if (sig.dist === 1 && fired.length) {
            banner("Distribution shift flagged: " + fired.join(", ") + " after "
              + "Benjamini-Hochberg correction. Alerts fire only when the verdict "
              + "repeats on consecutive cycles; this cycle is recorded either way.");
          } else if (sig.qualityChange || sig.guardrail) {
            banner("Quality signal flagged (response availability or the invalid share "
              + "guardrail). This is reported separately from the distribution "
              + "shift status.");
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
        // Errors surface as they happened, including explicit provider
        // unavailability; nothing hides behind a synthetic answer.
        note.textContent = "That run failed: " + (st.error || "unknown issue")
          + (String(st.error || "").includes("Provider unavailable")
            ? " New checks stay unavailable until the provider key is configured."
            : " Nothing was committed for it; wait a moment and try again.");
        await loadAll();
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
loadLive();        // fast liveness: pill says the process answers immediately
ensureAwake();     // data panels, each rendering individually as they land
/* Readiness is revalidated on its own slow cadence; panels never wait for it. */
setInterval(loadHealth, 60000);
/* The pill keeps counting while connecting, without touching panels. */
setInterval(() => {
  if (!state.awake && state.wakeStartedAt && !state.live) {
    setPill("pill-wake", `connecting · ${fmtWait(Date.now() - state.wakeStartedAt)}`);
  }
}, 1000);
/* Periodic refresh only when connected and visible; panels that failed one
   round are retried here automatically. If nothing answers anymore (service
   asleep), resume the wake loop. */
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) {
    loadAll();
    if (state.failed.size >= DATA_LOADERS.length) {
      state.awake = false;
      ensureAwake();
    }
  }
}, 60000);
