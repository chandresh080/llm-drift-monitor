/* Drift Monitor dashboard.
   Vanilla JS, no dependencies; every displayed value comes from the REST
   API on the same origin. Nothing is computed client side except formatting.

   Layout contract: Monitor opens with the action and latest verdict, followed
   by a two-column workspace. Detector results, history and recent activity
   lead the main column; response samples and category analysis stay secondary
   disclosures. Alerts and configuration sit in the side column. Evaluation
   and Demo are separate views.

   Loading contract: every data endpoint is fetched and rendered
   independently, so each panel appears at the earliest moment its own
   data is available. API status combines /health/live with evidence from
   the data endpoints: a failed liveness probe while data still answers is
   degraded, not offline. */

const state = {
  live: null,         // /health/live payload (fast liveness)
  liveError: null,    // last liveness failure, if any
  health: null,       // /health payload (DB + provider readiness)
  healthError: null,  // last readiness failure, with diagnostics attached
  healthCheckedAt: 0,
  cyclesLoaded: false, // /cycles answered: direct evidence the DB is readable
  config: null,       // /config payload
  stats: null,        // diagnostics: probe/cycle/test-result counts
  latest: null,       // /drift/latest payload
  cycles: [],         // /cycles: one row per monitoring cycle
  alerts: [],         // recent persisted alerts
  comparison: null,   // /drift/comparison
  categories: null,   // /drift/categories
  evaluation: null,   // /evaluation
  providerComparison: null,  // recorded Groq vs Gemini comparison (static asset)
  failed: new Set(),  // data endpoints that did not answer
  awake: false,       // at least one data endpoint has answered
  dataOk: false,      // at least one data endpoint returned a payload
  dataFailed: false,  // at least one data endpoint failed
  lastLoad: null,     // {ok, failed, at} for the latest full refresh
  wakeStartedAt: null,
};

/* Demo state is deliberately separate from live monitoring state. The Demo
   view reads one committed artifact and never calls /runs or any data
   endpoint that describes production storage. */
const demo = {
  artifact: null,       // validated static artifact
  scenarioId: null,
  cycleNumber: null,
  error: null,
  loaded: false,
  loading: false,
};

const DEMO_ARTIFACT_URL = "/static/data/demo-scenarios.json";

const WAKE_WAIT_MS = 5000;
const REQUEST_TIMEOUT_MS = 15000;
/* Match api.PROVIDER_HEALTH_TTL_SECONDS (60s). The API reuses a fresh ping
   for that long, so polling at this rate costs no extra provider quota and
   bounds how long an unresolved status can linger on screen. /config can
   override the value if the backend TTL changes. */
const DEFAULT_HEALTH_REFRESH_MS = 60000;
/* A failed readiness request gets one quick retry: a cold Render instance
   can outlast the browser timeout, then answer from its own 60s ping cache.
   Further retries use the normal TTL interval, so a broken deployment is not
   pinged in a tight loop. */
const HEALTH_RETRY_MS = 5000;

const $ = (id) => document.getElementById(id);

/* ---------- shared icon system ----------

   Every icon is the same 16px, 1.8-stroke, round-cap outline. Markup hosts
   declare data-icon; hydrateIcons() is the only place that writes the SVG, so
   the shell, tabs, tables and disclosures cannot drift apart. Decorative
   icons stay aria-hidden; controls keep their own text or aria-label. */
const ICONS = {
  activity: '<path d="M3 12h3.5l2.5-7 3.5 14 2.5-7H13"/>',
  server: '<rect x="3" y="3.5" width="10" height="4" rx="1"/>'
    + '<rect x="3" y="9.5" width="10" height="4" rx="1"/>'
    + '<path d="M6 5.5h.01M6 11.5h.01"/>',
  database: '<ellipse cx="8" cy="4.5" rx="4.5" ry="1.8"/>'
    + '<path d="M3.5 4.5v7c0 1 2 1.8 4.5 1.8s4.5-.8 4.5-1.8v-7"/>'
    + '<path d="M3.5 8c0 1 2 1.8 4.5 1.8s4.5-.8 4.5-1.8"/>',
  brain: '<path d="M8 4.4A2.4 2.4 0 0 0 3.5 5.7 2.4 2.4 0 0 0 4 10.4a2.4 2.4 0 0 0 4 2.2V4.4Z"/>'
    + '<path d="M8 4.4a2.4 2.4 0 0 1 4.5 1.3 2.4 2.4 0 0 1-.5 4.7 2.4 2.4 0 0 1-4 2.2V4.4Z"/>',
  github: '<path d="M8 1.8a6.2 6.2 0 0 0-2 12.1v-2.3c-1.7.4-2.1-.7-2.1-.7"/>'
    + '<path d="M4.4 11.5c-1.2-.3-1.7-.9-1.7-.9-.3 2 1.1 2.3 1.7 2.3v-1.4Z"/>'
    + '<path d="M6.2 9.2c-.1.3-.4.5-.7.4-.6-.2-.9-.1-.9-.1 0 .3.4.5.7.6.3.1.5.5.5.9.2.4.7.6 1.5.5.7-.1 1-1 1-1 .3-.5.8-.8 1.5-.8.5 0 .8-.1 1-.3-.1 1.4.4 2.5.4 2.5s-.5.3-.6 1.1c-.1 1 .3 1.4.3 1.4a6.2 6.2 0 1 0-3.2-11.7v2.2c0 .6-.5 1.1-1.1 1.1Z"/>',
  chart: '<path d="M3 13.5V3M3 13.5h10"/>'
    + '<path d="M5.7 10.5 8 7.6l2.1 1.8 2.7-4"/>',
  flask: '<path d="M6.4 2.5h3.2M7.2 2.5v5L3.6 14a1.8 1.8 0 0 0 1.6 2.7h5.6A1.8 1.8 0 0 0 12.4 14L8.8 7.5v-5"/>'
    + '<path d="M5.5 12h5"/>',
  radar: '<circle cx="8" cy="8" r="5.4"/><circle cx="8" cy="8" r="2"/>'
    + '<path d="M8 8 12.2 3.8M8 1.8v1.4M8 12.8v1.4M1.8 8h1.4M12.8 8h1.4"/>',
  history: '<path d="M3 8a5 5 0 1 1 1.5 4.5"/><path d="M3 4.5V8h3.5"/>'
    + '<path d="M8 5.5V8l2 1.3"/>',
  layers: '<path d="m8 2.5 5.5 3L8 8.5 2.5 5.5 8 2.5Z"/>'
    + '<path d="m2.5 9 5.5 3 5.5-3M2.5 12.3 8 15.3l5.5-3"/>',
  network: '<circle cx="3.5" cy="4" r="1.7"/><circle cx="12.5" cy="4" r="1.7"/>'
    + '<circle cx="8" cy="12" r="1.7"/><path d="M4.9 5.2 7 10.6M11.1 5.2 9 10.6M5.2 4h5.6"/>',
  bell: '<path d="M4.5 11.5V7a3.5 3.5 0 0 1 7 0v4.5l1.3 2H3.2l1.3-2Z"/>'
    + '<path d="M6.7 13.5a1.3 1.3 0 0 0 2.6 0"/>',
  settings: '<circle cx="8" cy="8" r="2.2"/>'
    + '<path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M12.4 3.6l-1.2 1.2M4.8 11.2l-1.2 1.2"/>',
  book: '<path d="M8 4.2C6.6 3 4.9 2.8 2.8 3.2v9c2.1-.4 3.8-.2 5.2 1 1.4-1.2 3.1-1.4 5.2-1v-9c-2.1-.4-3.8-.2-5.2 1Z"/>'
    + '<path d="M8 4.2v9"/>',
  play: '<path d="M5 3.5 12.5 8 5 12.5V3.5Z"/>',
  check: '<circle cx="8" cy="8" r="6"/><path d="m5.2 8.2 1.9 1.9 3.9-4.1"/>',
  alert: '<path d="M8 2.4 14.4 13H1.6L8 2.4Z"/><path d="M8 6v3.2M8 11h.01"/>',
  "chevron-right": '<path d="m6 4 4 4-4 4"/>',
  clock: '<circle cx="8" cy="8" r="6"/><path d="M8 4.8V8l2.2 1.5"/>',
  dash: '<circle cx="8" cy="8" r="6"/><path d="M5.3 8h5.4"/>',
};

function iconSvg(name, className = "ui-icon") {
  return `<svg class="${className}" viewBox="0 0 16 16" aria-hidden="true" `
    + `focusable="false">${ICONS[name] || ICONS.dash}</svg>`;
}

function hydrateIcons() {
  if (!document.querySelectorAll) return;
  document.querySelectorAll("[data-icon]").forEach((host) => {
    host.innerHTML = iconSvg(host.dataset ? host.dataset.icon : "");
    if (host.dataset) host.dataset.iconReady = "1";
  });
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtTime(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return "n/a";
  const opts = {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  };
  /* An old cycle must show its actual date, not imply currentness. */
  if (t.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return t.toLocaleString(undefined, opts);
}

function fmtP(p) {
  if (p == null) return "n/a";
  return Number(p) < 0.001 ? "< 0.001" : Number(p).toFixed(3);
}

/* Artifact fields are machine keys. Show their meaning, never the key:
   `paired_permutation` becomes "Paired permutation" and `drift` becomes
   "Drift". Known statistical methods get their full names once, here. */
const METHOD_LABELS = {
  ks: "Kolmogorov–Smirnov",
  fisher: "Fisher exact",
  z: "Z test",
  proportion_z: "Proportion z",
  paired_permutation: "Paired permutation",
  mmd: "Maximum mean discrepancy",
  bh: "Benjamini–Hochberg",
  benjamini_hochberg: "Benjamini–Hochberg",
  "benjamini-hochberg": "Benjamini–Hochberg",
};

function humanizeKey(value) {
  const key = String(value == null ? "" : value).trim();
  if (!key) return "n/a";
  if (METHOD_LABELS[key.toLowerCase()]) return METHOD_LABELS[key.toLowerCase()];
  return key
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^[a-z]/, (c) => c.toUpperCase());
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
      err.isTimeout = false;
      err.status = resp.status;
      err.retryAfter = resp.headers.get("Retry-After");
      try {
        const body = await resp.json();
        if (body && body.detail) err.detail = body.detail;
      } catch (e) { /* non json error body: status is explanation enough */ }
      throw err;
    }
    return await resp.json();
  } catch (err) {
    // AbortController fires AbortError when our own deadline expires. Mark it
    // so readiness can tell a timeout from a server/network error.
    if (err && err.name === "AbortError") err.isTimeout = true;
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/* ---------- service status ---------- */

/* Three independent facts, three separate indicators:
   - API reachability: liveness plus the stronger evidence of data endpoints.
     A timed-out liveness probe while data still answers is not degradation.
   - Database availability: /health's db field.
   - Provider availability: /health's provider_status (Gemini).
   A transient liveness failure before any data answers stays unknown until
   there is enough evidence to classify the service. */
function apiState() {
  const liveOk = state.live != null && state.live.status === "ok";
  const liveKnown = state.live != null;
  if (liveOk) {
    return state.dataFailed && !state.dataOk ? "degraded" : "online";
  }
  if (state.dataOk) return "online";
  if (liveKnown && state.dataFailed) return "offline";
  if (liveKnown) return "unknown";
  if (state.dataFailed) return "unknown";
  return "checking";
}

const API_STATES = {
  online: ["status-ok", "API Online", "The API is answering."],
  degraded: ["status-warn", "API Degraded",
    "The API process answered, but its data endpoints did not."],
  offline: ["status-bad", "API Offline", "The API is not answering."],
  unknown: ["status-muted", "API Unknown",
    "The liveness check failed before any data endpoint answered."],
  checking: ["status-muted", "API Checking", "Waiting for the first response."],
};

/* Connectivity and persistence are separate facts:
   - connectivity comes from any endpoint that actually queried storage,
     not from /health alone;
   - persistence comes from config.storage (backend + durable), never from
     the word "available". SQLite on the Render free filesystem is
     ephemeral even though its queries succeed. */
function dbState() {
  const h = state.health;
  // A successful /health response is authoritative for connectivity: it
  // just ran a real storage query and reported the result.
  if (h) {
    return (h.database === "available" || h.db === "ok")
      ? "available" : "unavailable";
  }
  // /health did not answer. A DB-backed endpoint that did answer is direct
  // evidence the database is reachable, so never call it Unknown then.
  if (state.stats != null || state.cyclesLoaded) return "available";
  if (state.config && state.config.storage && state.config.storage.backend) {
    return "available";
  }
  if (state.healthError) return "unknown";
  return "checking";
}

/* Compact backend identity, shown next to the DB state and in Configuration.
   "Persistent" is only claimed for a configured PostgreSQL backend that the
   service itself reports durable; the plan caveat stays in the tooltip. */
function storageLabel() {
  const st = state.config && state.config.storage;
  if (!st || !st.backend) return "";
  const backend = String(st.backend);
  const name = backend.indexOf("postgres") === 0 ? "PostgreSQL"
    : (backend === "sqlite" || backend === "sqlite-fallback") ? "SQLite"
      : backend;
  const persistent = backend.indexOf("postgres") === 0 && st.durable === true;
  return persistent ? `Persistent ${name}` : `Temporary ${name}`;
}

const DB_STATES = {
  available: ["status-ok", "Database Available",
    "The storage backend answered a query."],
  unavailable: ["status-bad", "Database Unavailable",
    "The storage backend did not answer."],
  unknown: ["status-muted", "Database Unknown",
    "Readiness and every database-backed endpoint failed; state is unknown."],
  checking: ["status-muted", "Database Checking",
    "Waiting for the readiness check."],
};

function providerName() {
  const model = modelId();
  if (model && /gemini/i.test(model)) return "Gemini";
  if (state.config && state.config.adapter_type) {
    return String(state.config.adapter_type).replace(/Adapter$/, "") || "Provider";
  }
  return "Gemini";
}

/* The model is normally on /config. A deployment without provider
   credentials cannot build the adapter, so /config has no target_model;
   readiness still names the configured model, and the header keeps it. */
function modelId() {
  const c = state.config;
  if (c && c.target_model) return c.target_model;
  if (state.health && state.health.provider) return state.health.provider;
  return "";
}

function providerState() {
  if (state.health) {
    const status = state.health.provider_status;
    if (status === "ok") return "available";
    // Keep the backend's distinction: `unreachable` is a measured failure,
    // `not_configured` is a missing key, `unchecked` was never pinged, and
    // only a value we do not recognise is genuinely unknown.
    if (status === "unreachable") return "unavailable";
    if (status === "not_configured") return "not_configured";
    if (status === "unchecked") return "unchecked";
    return "unknown";
  }
  if (state.config && state.config.provider_available === false) return "unavailable";
  if (state.healthError) return "unknown";
  return "checking";
}

/* One shared explanation backs both the provider pill tooltip and the model
   label tooltip, so the header cannot say two different things. The reason is
   built from fixed vocabulary plus sanitised values: a client timeout, the
   numeric HTTP status, `unchecked`, or the literal unrecognised status. No
   response body or credential-bearing text is repeated here. */
function providerTooltip() {
  const h = state.health;
  const status = h && h.provider_status;
  const err = state.healthError;

  if (err) {
    if (err.timeout) {
      return "Provider readiness timed out before a response arrived; "
        + "no provider status was returned.";
    }
    if (err.httpStatus) {
      return `Provider readiness request returned HTTP ${err.httpStatus}; `
        + "no provider status was available.";
    }
    const kind = String(err.kind || "network error")
      .replace(/[^A-Za-z0-9 ._-]/g, "").slice(0, 48);
    return `Provider readiness request failed (${kind}); no provider status was available.`;
  }

  if (status === "ok") return "The target provider answered its readiness ping.";
  if (status === "unchecked") {
    return "The readiness response reported provider_status 'unchecked'; "
      + "the provider was not pinged.";
  }
  if (status === "not_configured") {
    return (h && h.provider_error)
      || (state.config && state.config.provider_error)
      || "The target provider is not configured; new checks cannot run.";
  }
  if (status === "unreachable") {
    return (h && h.provider_error)
      || (state.config && state.config.provider_error)
      || "The target provider cannot run new cycles.";
  }
  if (status) {
    const shown = String(status).replace(/[^A-Za-z0-9._:-]/g, "").slice(0, 40);
    return `The readiness response reported an unrecognised provider status: '${shown}'.`;
  }
  if (state.config && state.config.provider_available === false) {
    return (state.config && state.config.provider_error)
      || "The target provider cannot run new cycles.";
  }
  return "Waiting for the readiness check.";
}

function providerStates() {
  const name = providerName();
  const detail = providerTooltip();
  return {
    available: ["status-ok", `${name} Available`, detail],
    unavailable: ["status-bad", `${name} Unavailable`, detail],
    not_configured: ["status-warn", `${name} Not Configured`, detail],
    unchecked: ["status-muted", `${name} Unchecked`, detail],
    unknown: ["status-muted", `${name} Unknown`, detail],
    checking: ["status-muted", `${name} Checking`, detail],
  };
}

const STATUS_ICONS = {
  "health-pill": "server",
  "db-pill": "database",
  "provider-pill": "brain",
};

function renderPill(id, found) {
  const pill = $(id);
  if (!pill) return;
  pill.className = `status ${found[0]}`.trim();
  /* Rebuild from the shared icon system: the host is declared in the shell
     and the text stays in its own span for responsive labels. */
  pill.innerHTML = iconSvg(STATUS_ICONS[id] || "dash")
    + `<span class="status-text">${escapeHtml(found[1])}</span>`;
  pill.title = found[2];
}

function renderStatusGroup() {
  renderPill("health-pill", API_STATES[apiState()] || API_STATES.checking);
  /* The header pill reports readiness only. Storage durability and blocked
     writes belong to the one collapsed methodology section, not a tooltip. */
  renderPill("db-pill", DB_STATES[dbState()] || DB_STATES.checking);
  const states = providerStates();
  renderPill("provider-pill", states[providerState()] || states.checking);
}

/* Kept as the single render entry point used by data loaders. */
function renderHealthPill() {
  renderStatusGroup();
}

/* ---------- model label in the header ---------- */

/* Human label for known Gemini ids; unknown ids stay exactly as served. */
function prettyModel(id) {
  return String(id || "")
    .replace(/^gemini-/, "Gemini ")
    .replace(/-flash-lite$/, " Flash-Lite");
}

function renderModelLabel() {
  const c = state.config;
  const el = $("model-label");
  if (!el) return;
  const id = modelId();
  if (!id) {
    el.textContent = "";
    el.className = "brand-model";
    el.removeAttribute("title");
    return;
  }
  /* Provider availability has its own header indicator; the model label
     stays a label. Its tooltip always reuses the exact readiness explanation
     shown on the pill, so `unchecked` is never presented as a failure. */
  el.className = "brand-model";
  el.textContent = prettyModel(id);
  el.title = providerTooltip();
}

function initTabs() {
  const show = (view) => {
    $("tab-console").hidden = view !== "console";
    $("tab-evaluation").hidden = view !== "evaluation";
    $("tab-demo").hidden = view !== "demo";
    $("tab-btn-console").classList.toggle("tab-active", view === "console");
    $("tab-btn-evaluation").classList.toggle("tab-active", view === "evaluation");
    $("tab-btn-demo").classList.toggle("tab-active", view === "demo");
    for (const [button, active] of [
      ["tab-btn-console", view === "console"],
      ["tab-btn-evaluation", view === "evaluation"],
      ["tab-btn-demo", view === "demo"],
    ]) {
      if (active) $(button).setAttribute("aria-current", "page");
      else $(button).removeAttribute("aria-current");
    }
    if (view === "evaluation") {
      if (state.evaluation) {
        try { renderEvaluation(); } catch (err) { /* blocks render independently */ }
      } else {
        loadEvaluation();
      }
    }
    if (view === "demo") loadDemoArtifact();
  };
  $("tab-btn-console").addEventListener("click", () => show("console"));
  $("tab-btn-evaluation").addEventListener("click", () => show("evaluation"));
  $("tab-btn-demo").addEventListener("click", () => show("demo"));
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
  const degradation = l.quality_degradation != null
    ? l.quality_degradation : l.overall_quality_degradation;
  const guardrail = Number(degradation) === 1;
  return { dist, qualityChange: Boolean(qualityChange), guardrail };
}

/* ---------- verdict ---------- */

function distributionFlagCount(latest) {
  if (!latest) return null;
  const flags = [latest.ks_significant, latest.mmd_significant];
  if (flags.some((v) => v != null)) {
    return flags.filter((v) => Number(v) === 1).length;
  }
  return null;
}

function renderVerdict() {
  const latest = state.latest;
  const title = $("kpi-drift");
  const sub = $("verdict-sub");
  const meta = $("verdict-meta");
  const strip = $("quality-strip");
  const refEl = $("count-ref");
  const curEl = $("count-cur");
  const evaluated = latest && (latest.drift_detected != null
    || latest.distribution_shift_detected != null);

  if (!evaluated) {
    title.textContent = "Building baseline";
    title.className = "verdict";
    sub.textContent = "Waiting for a completed check.";
    meta.textContent = "";
    if (refEl) refEl.textContent = "n/a";
    if (curEl) curEl.textContent = "n/a";
    strip.hidden = true;
    strip.className = "quality-signal";
    strip.innerHTML = "";
    return;
  }

  const sig = latestSignals(latest);
  const fired = distributionFlagCount(latest);
  const qualityIssue = sig.guardrail || sig.qualityChange;
  title.textContent = sig.dist === 1 ? "Response pattern changed"
    : (sig.guardrail ? "Too many responses failed"
      : (sig.qualityChange ? "Fewer responses succeeded"
        : "No significant change"));
  title.className = "verdict" + (sig.dist === 1 || qualityIssue ? " verdict-alert" : "");
  sub.textContent = fired == null
    ? "Response pattern checks not run"
    : `${fired} of 2 response pattern checks flagged`;

  meta.textContent = latest.timestamp ? `Last checked ${fmtTime(latest.timestamp)}` : "";
  if (refEl) {
    refEl.textContent = latest.reference_size == null
      ? "n/a" : Number(latest.reference_size).toLocaleString();
  }
  if (curEl) {
    curEl.textContent = latest.current_size == null
      ? "n/a" : Number(latest.current_size).toLocaleString();
  }

  /* Response problems stay separate from the pattern verdict. State the
     problem first so a stable pattern cannot hide failed requests. */
  const reasons = [];
  if (sig.qualityChange) reasons.push("Fewer responses succeeded than in the baseline.");
  if (sig.guardrail) reasons.push("Too many responses failed during this check.");
  const qualityKnown = latest.quality_change_detected != null
    || latest.quality_degradation != null || latest.overall_quality_degradation != null;
  if (!qualityKnown) {
    strip.hidden = true;
    strip.className = "quality-signal";
    strip.innerHTML = "";
  } else if (reasons.length) {
    strip.hidden = false;
    strip.className = "quality-signal is-bad";
    strip.innerHTML = iconSvg("alert")
      + '<span class="quality-name">Responses</span>'
      + `<span class="quality-state">${sig.guardrail
        ? "Response failures" : "Response problem"}</span>`
      + `<span class="quality-reason">${escapeHtml(reasons.join(" "))}</span>`;
  } else {
    strip.hidden = false;
    strip.className = "quality-signal is-ok";
    strip.innerHTML = iconSvg("check")
      + '<span class="quality-name">Responses</span>'
      + '<span class="quality-state">Working</span>';
  }
}

/* ---------- stored response count ---------- */

function renderMetrics() {
  const runsEl = $("kpi-runs");
  if (!runsEl) return;
  runsEl.textContent = state.stats && state.stats.probe_runs != null
    ? Number(state.stats.probe_runs).toLocaleString() : "n/a";
}

/* ---------- detector table ---------- */

/* One row per detector: name with its method as secondary text, raw and
   adjusted p-values, decision. Absent values decide as "Not evaluated",
   never as "No flag". Availability is a quality signal, so its flag is
   labelled as such. Every state reserves the same icon slot so flagged,
   unflagged and unevaluated rows share one geometry. */
function decisionState(p, sig, quality) {
  if (p == null || sig == null) {
    return { text: "Not evaluated", cls: "decision decision-muted",
      mark: iconSvg("dash") };
  }
  if (Number(sig) === 1) {
    return {
      text: quality ? "Flagged (quality)" : "Flagged",
      cls: "decision decision-alert",
      mark: iconSvg("alert"),
    };
  }
  return { text: "No flag", cls: "decision", mark: iconSvg("check") };
}

function renderDetectors() {
  const l = state.latest;
  const rows = [
    {
      name: "Response length",
      method: "KS",
      raw: l ? l.ks_pvalue : null,
      p: l ? l.ks_adjusted_pvalue : null,
      sig: l ? l.ks_significant : null,
      quality: false,
    },
    {
      name: "Response availability",
      method: l && l.proportion_z_test_method === "fisher" ? "Fisher exact"
        : "Fisher / z",
      raw: l ? l.proportion_z_pvalue : null,
      p: l ? l.proportion_z_adjusted_pvalue : null,
      sig: l ? l.proportion_z_significant : null,
      quality: true,
    },
    {
      name: "Semantic shift",
      method: "Paired MMD",
      raw: l ? l.mmd_pvalue : null,
      p: l ? l.mmd_adjusted_pvalue : null,
      sig: l ? l.mmd_significant : null,
      quality: false,
    },
  ];
  $("detector-rows").innerHTML = rows.map((r) => {
    const d = decisionState(r.p, r.sig, r.quality);
    return `
      <tr data-testid="detector-row">
        <td>
          <span class="detector-name">${escapeHtml(r.name)}</span>
          <span class="detector-method">${escapeHtml(r.method)}</span>
        </td>
        <td class="num">${r.raw == null ? "n/a" : fmtP(r.raw)}</td>
        <td class="num">${r.p == null ? "n/a" : fmtP(r.p)}</td>
        <td><span class="${d.cls}">${d.mark}${d.text}</span></td>
      </tr>`;
  }).join("");
}

/* ---------- effect sizes ---------- */

function renderEffects() {
  const l = state.latest || {};
  const set = (id, value) => {
    const el = $(id);
    if (value == null) {
      el.textContent = "n/a";
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

/* A configured PostgreSQL database that failed to initialize leaves a
   read-only SQLite fallback. New checks are refused by the API in that state
   and the button must explain why instead of looking temporarily busy. */
function runBlockedReason() {
  const sources = [state.config && state.config.storage,
    state.health && state.health.storage];
  for (const st of sources) {
    if (!st) continue;
    if (st.new_checks_enabled === false || st.accepts_writes === false
        || st.fallback === true || st.backend === "sqlite-fallback") {
      return "New checks are disabled: persistent storage is unavailable. "
        + "Existing history stays readable; fix DATABASE_URL and redeploy.";
    }
  }
  return null;
}

/* The operational note next to the Run check button uses the real
   configured values: how many prompts a run collects, the per-visitor
   interval, and the remaining daily quota. It never hardcodes numbers
   another deployment would contradict. */
function renderRunNote() {
  const c = state.config;
  const blocked = runBlockedReason();
  const btn = $("run-btn");
  if (btn) btn.disabled = Boolean(blocked);
  if (blocked) {
    const note = $("run-note");
    note.dataset.active = "1";
    note.textContent = blocked;
    return;
  }
  if (!c || $("run-note").dataset.active) return;
  const rp = c.run_policy || {};
  const bits = [`${c.probes_per_cycle || c.prompt_panel_probes || "?"} prompts per run`];
  if (rp.per_visitor_interval_seconds != null) {
    bits.push(`1 check per visitor every ${Math.round(rp.per_visitor_interval_seconds / 60)} min`);
  }
  if (rp.daily_cycle_budget != null) {
    const used = rp.daily_cycles_used_today != null ? Number(rp.daily_cycles_used_today) : 0;
    const left = Math.max(0, Number(rp.daily_cycle_budget) - used);
    bits.push(left > 0
      ? `${left} of ${rp.daily_cycle_budget} checks left today`
      : "daily check budget used");
  }
  $("run-note").textContent = bits.join(". ") + ".";
}

/* Compact values only: no explanatory sentences next to a field. Method
   names travel with the value when they matter. Embedding model and
   baseline manifest stay available as secondary metadata below the
   columns. */
function renderConfig() {
  const c = state.config;
  if (!c) {
    $("cfg-model").textContent = state.failed.has("config")
      ? "unavailable" : "…";
    renderModelLabel();
    return;
  }
  $("cfg-model").textContent = modelId() || "unavailable";
  $("cfg-panel").textContent =
    `${c.probes_per_cycle} prompts per cycle, panel v${c.prompt_panel_version}`;
  const cyclesForWindow = c.probes_per_cycle
    ? Math.round(c.reference_window / c.probes_per_cycle) : null;
  $("cfg-ref").textContent = cyclesForWindow
    ? `${c.reference_window} responses (${cyclesForWindow} cycles)`
    : `${c.reference_window} responses`;
  $("cfg-cur").textContent = `${c.current_window} responses`;
  $("cfg-alpha").textContent =
    `${Number(c.alpha).toFixed(2)}, Benjamini–Hochberg`;
  $("cfg-persist").textContent = `${c.min_consecutive_cycles} changed cycles in a row`;
  const storageEl = $("cfg-storage");
  if (storageEl) {
    storageEl.textContent = storageLabel() || "unknown";
    storageEl.title = [c.storage && c.storage.note, runBlockedReason()]
      .filter(Boolean).join(" ");
  }

  const l = state.latest || {};
  const ts = state.latest && state.latest.timestamp;
  $("cfg-last-run").textContent = ts ? fmtTime(ts) : "none yet";
  $("cfg-latency").textContent = l.provider_latency_mean_seconds == null
    ? "not recorded"
    : `${Number(l.provider_latency_mean_seconds).toFixed(1)} s mean, `
      + `${Number(l.provider_latency_p95_seconds).toFixed(1)} s p95`;
  $("cfg-tokens").textContent = l.probe_tokens_total == null
    ? "not recorded" : `${Number(l.probe_tokens_total).toLocaleString()} tokens`;

  $("cfg-embedding").textContent = c.embedding_model
    ? `Embedding model: ${c.embedding_model}` : "";
  const m = c.baseline_manifest;
  $("cfg-manifest").textContent = m
    ? `Baseline: ${m.provider} ${m.model_id}, panel v${m.panel_version}, `
      + `temperature ${m.temperature}, collected ${fmtTime(m.collected_at)}`
    : "";
  renderRunNote();
  renderModelLabel();
}

/* ---------- response samples ---------- */

/* Inspection tool, not a dump: compact grid rows, responses whose text
   actually changed first, five rows until the visitor asks for all, and
   the full text only inside a row's detail panel. */
let samplesShowAll = false;
let samplesCategory = "all";

function sameResponse(a, b) {
  const at = a && a.text != null ? String(a.text) : null;
  const bt = b && b.text != null ? String(b.text) : null;
  return at === bt;
}

function sampleLength(data) {
  if (!data || data.text == null) return null;
  return data.length != null ? Number(data.length) : String(data.text).length;
}

/* Inline 16px status icon: green check for a non-empty response, red cross
   for an empty/failed one, neutral dash when validity was never recorded.
   The icon carries its own accessible label and tooltip, so the meaning does
   not depend on colour alone. */
function validityIcon(kind) {
  const icon = { ok: "check", bad: "alert", neutral: "dash" }[kind] || "dash";
  const labels = {
    ok: "Non-empty response",
    bad: "Empty response",
    neutral: "Validity not recorded",
  };
  const label = labels[kind] || labels.neutral;
  return `<span class="s-icon s-${kind}" role="img" aria-label="${label}" `
    + `title="${label}">${iconSvg(icon)}</span>`;
}

function sampleValidity(data) {
  if (!data || data.is_valid == null) return validityIcon("neutral");
  return validityIcon(Number(data.is_valid) === 1 ? "ok" : "bad");
}

/* Stored text can be null (not captured) or the empty string (the provider
   returned nothing). Say which it is instead of showing a blank box. */
function respText(data) {
  if (!data || data.text == null) return "n/a";
  return String(data.text).length ? String(data.text) : "(empty response)";
}

function sampleCells(r) {
  const refLen = sampleLength(r.reference);
  const curLen = sampleLength(r.current);
  return {
    ref: `${refLen == null
      ? '<span class="s-missing">n/a</span>' : `${refLen} ch`} ${sampleValidity(r.reference)}`,
    current: `${curLen == null
      ? '<span class="s-missing">n/a</span>' : `${curLen} ch`} ${sampleValidity(r.current)}`,
    changed: !sameResponse(r.reference, r.current),
    delta: (refLen != null && curLen != null) ? curLen - refLen : null,
  };
}

function changeCell(cells, redacted) {
  if (redacted) return '<span class="s-missing">withheld</span>';
  if (!cells.changed) return '<span class="s-state">unchanged</span>';
  const d = cells.delta;
  const delta = d == null ? "" :
    `<span class="s-delta">${d > 0 ? "+" : ""}${d} ch</span>`;
  return `<span class="s-changed">changed</span>${delta}`;
}

function renderComparison() {
  const wrap = $("comparison-wrap");
  const count = $("sample-count");
  const c = state.comparison;
  if (c == null) {
    if (count) count.textContent = "";
    wrap.innerHTML = `<p class="empty-state">${
      state.failed.has("comparison")
        ? "Comparison endpoint unavailable." : "Comparison unavailable."}</p>`;
    return;
  }
  if (!c.ready) {
    if (count) count.textContent = "";
    wrap.innerHTML = `
      <p class="empty-state">Evaluation has not started yet: ${c.reference_stored} of
      ${c.reference_required} reference and ${c.current_stored} of ${c.current_required}
      current responses stored.</p>`;
    return;
  }
  const all = c.rows || [];
  if (count) count.textContent = `${all.length} prompts`;

  const categories = Array.from(new Set(all.map((r) => r.category).filter(Boolean)))
    .sort();
  if (samplesCategory !== "all" && !categories.includes(samplesCategory)) {
    samplesCategory = "all";
  }
  const filtered = samplesCategory === "all"
    ? all
    : all.filter((r) => r.category === samplesCategory);

  const decorated = filtered.map((r) => ({
    r,
    cells: sampleCells(r),
    redacted: Boolean(r.prompt_redacted),
  }));
  /* Genuinely changed responses first; the sort is stable, so the stored
     order survives inside each group. */
  decorated.sort((a, b) => Number(b.cells.changed) - Number(a.cells.changed));

  const visible = samplesShowAll ? decorated : decorated.slice(0, 5);
  const rows = visible.map(({ r, cells, redacted }) => {
    const label = r.label || r.prompt_text || r.prompt_id || "prompt";
    const detail = redacted
      ? `<p class="sample-withheld">Safety boundary probe: the exact request and
           the model's answers are not published. Lengths and validity flags
           remain visible above.</p>`
      : `
        <p class="prompt-line">${escapeHtml(r.prompt_text || "")}</p>
        <div class="sample-cols">
          <div class="sample-col">
            <h4>Reference</h4>
            <div class="resp-line">
              <code class="resp-text">${escapeHtml(respText(r.reference))}</code>
              <span class="resp-icon">${sampleValidity(r.reference)}</span>
            </div>
          </div>
          <div class="sample-col">
            <h4>Current</h4>
            <div class="resp-line">
              <code class="resp-text">${escapeHtml(respText(r.current))}</code>
              <span class="resp-icon">${sampleValidity(r.current)}</span>
            </div>
          </div>
        </div>`;
    return `
      <details class="sample-row" data-redacted="${redacted ? "1" : "0"}"
        data-changed="${cells.changed ? "1" : "0"}">
        <summary class="sample-grid">
          <span class="s-prompt">
            <span class="s-label">${escapeHtml(label)}</span>
            ${r.category ? `<span class="s-cat">${escapeHtml(r.category)}</span>` : ""}
            ${redacted ? '<span class="s-flag">withheld</span>' : ""}
          </span>
          <span class="s-num">${cells.ref}</span>
          <span class="s-num">${cells.current}</span>
          <span class="s-change">${changeCell(cells, redacted)}</span>
        </summary>
        <div class="sample-detail">${detail}</div>
      </details>`;
  }).join("");

  const controls = `
    <div class="samples-controls">
      <span class="samples-visible">${visible.length} of ${filtered.length} shown</span>
      ${categories.length > 1 ? `
        <select id="samples-category" class="samples-select" aria-label="Filter samples by category">
          <option value="all">All categories</option>
          ${categories.map((x) => `<option value="${escapeHtml(x)}"${
            x === samplesCategory ? " selected" : ""}>${escapeHtml(x)}</option>`).join("")}
        </select>` : ""}
    </div>
    <div class="sample-grid sample-head">
      <span>Prompt</span><span>Reference</span><span>Current</span><span>Change</span>
    </div>`;

  const toggle = filtered.length > 5 ? `
    <button id="samples-toggle" class="link-btn" type="button">
      ${samplesShowAll ? "Show fewer" : `Show all ${filtered.length}`}
    </button>` : "";

  wrap.innerHTML = `${controls}
    <div class="sample-list">${rows}</div>
    ${toggle}`;

  const toggleEl = $("samples-toggle");
  if (toggleEl) {
    toggleEl.addEventListener("click", () => {
      samplesShowAll = !samplesShowAll;
      renderComparison();
    });
  }
  const selectEl = $("samples-category");
  if (selectEl) {
    selectEl.addEventListener("change", (event) => {
      samplesCategory = event && event.target ? event.target.value : "all";
      renderComparison();
    });
  }
}

/* ---------- category analysis ---------- */

function renderCategories() {
  const wrap = $("categories-wrap");
  const section = $("categories-section");
  const c = state.categories;
  if (c == null || !Array.isArray(c.rows) || c.rows.length === 0) {
    /* No empty disclosure in the workspace. */
    if (section) section.hidden = true;
    wrap.innerHTML = "";
    return;
  }
  if (section) section.hidden = false;
  const rows = c.rows.map((r) => {
    const tested = r.decision === "tested";
    const decision = tested
      ? `<span class="cat-decision${r.significant ? " is-alert" : ""}">`
        + `${r.significant ? "Flagged" : "No flag"}</span>`
        + ` <span class="muted">p ${fmtP(r.adjusted_pvalue)}</span>`
      : `<span class="muted">Not tested. `
        + `${escapeHtml(r.reason || "not enough distinct samples")}</span>`;
    return `
      <div class="cat-row">
        <span class="cat-name">${escapeHtml(r.category)}</span>
        <span>${decision}</span>
        <span class="cat-counts muted">${r.unique_reference}/${r.n_reference} reference and
          ${r.unique_current}/${r.n_current} current distinct</span>
      </div>`;
  }).join("");
  wrap.innerHTML = `<div>${rows}</div>`;
}

/* ---------- recent activity ---------- */

/* One row per real monitoring cycle, grouped from probe records by cycle id
   in the API. The old feed listed detector results, so a cycle that stored
   probes but was never evaluated simply vanished. Here a cycle without a
   result is still visible and says it is awaiting evaluation; a cycle with
   failed probes is not silently presented as a successful one. */
function cycleSignals(result) {
  if (!result) return { dist: null, availability: false, guardrail: false };
  return {
    dist: result.distribution_shift_detected != null
      ? Number(result.distribution_shift_detected)
      : (Number(result.ks_significant) === 1
        || Number(result.mmd_significant) === 1 ? 1 : 0),
    availability: Number(result.quality_change_detected != null
      ? result.quality_change_detected : result.proportion_z_significant) === 1,
    guardrail: Number(result.overall_quality_degradation != null
      ? result.overall_quality_degradation : result.quality_degradation) === 1,
  };
}

function cycleStatus(c) {
  switch (c.status) {
    case "evaluated": return ["Complete", "act-status is-ok"];
    case "reference_baseline": return ["Baseline", "act-status is-baseline"];
    case "awaiting_evaluation": return ["Waiting", "act-status is-wait"];
    case "failed": return ["Failed", "act-status is-bad"];
    case "interrupted": return ["Interrupted", "act-status is-warn"];
    default: return [humanizeKey(c.status || "recorded"), "act-status"];
  }
}

function alertsForCycle(cycle) {
  if (!cycle) return [];
  if (cycle.cycle_id) {
    const exact = (state.alerts || []).filter(
      (a) => a.cycle_id && a.cycle_id === cycle.cycle_id);
    if (exact.length) return exact;
  }
  const timestamp = cycle.ended_at || cycle.started_at || cycle.timestamp;
  if (!timestamp) return [];
  const key = String(timestamp).slice(0, 19);
  return (state.alerts || []).filter((a) =>
    String(a.timestamp || "").slice(0, 19) === key);
}

function cycleForAlert(alert) {
  if (!alert) return null;
  if (alert.cycle_id) {
    const exact = (state.cycles || []).find(
      (c) => c.cycle_id && c.cycle_id === alert.cycle_id);
    if (exact) return exact;
  }
  const timestamp = String(alert.timestamp || "").slice(0, 19);
  if (!timestamp) return null;
  return (state.cycles || []).find((c) => {
    const stamp = c.ended_at || c.started_at || c.timestamp;
    return stamp && String(stamp).slice(0, 19) === timestamp;
  }) || null;
}

function alertType(alert) {
  return String((alert && (alert.alert_type || alert.type)) || "").toLowerCase();
}

function alertKindLabel(alert) {
  const type = alertType(alert);
  if (type === "drift") return "Response pattern changed";
  if (type === "quality_degradation") return "Response problem";
  return "Check result";
}

/* The stored message and severity stay untouched in state. This only maps
   the alert family and its real counts to words a visitor can act on. */
function alertCopy(alert, sourceCycle) {
  const cycle = sourceCycle || cycleForAlert(alert);
  const result = (cycle && cycle.result) || {};
  const type = alertType(alert);
  const degradation = alert && alert.overall_quality_degradation != null
    ? alert.overall_quality_degradation
    : (result.overall_quality_degradation != null
      ? result.overall_quality_degradation : result.quality_degradation);
  const failedNow = Number(degradation) === 1;
  const changeFlag = result.quality_change_detected != null
    ? result.quality_change_detected : result.proportion_z_significant;
  const fewerSucceeded = Number(changeFlag) === 1;
  const items = [];

  if (type === "drift") {
    items.push({
      title: "Response pattern changed",
      text: "The model's response patterns changed compared with the baseline.",
      kind: "pattern",
    });
  }
  if (type === "quality_degradation") {
    if (fewerSucceeded) {
      items.push({
        title: "Fewer responses succeeded",
        text: "Fewer responses succeeded than in the baseline.",
        kind: "response",
      });
    }
    if (failedNow) {
      const total = Number(cycle && cycle.probe_count);
      const failed = Number(cycle && cycle.failed_count);
      const counted = Boolean(cycle && cycle.probe_count != null
        && cycle.failed_count != null && Number.isFinite(total) && total > 0
        && Number.isFinite(failed));
      items.push({
        title: "Response failures",
        text: counted
          ? `${failed} of ${total} responses failed during this check. `
            + "Review the failed responses before interpreting the monitoring results."
          : "Too many responses failed during this check.",
        kind: "failures",
      });
    }
    if (!items.length) {
      items.push({
        title: "Fewer responses succeeded",
        text: "Fewer responses succeeded than in the baseline.",
        kind: "response",
      });
    }
  }
  if (!items.length) {
    items.push({
      title: alertKindLabel(alert),
      text: "The check reported a result worth reviewing.",
      kind: "other",
    });
  }
  return items;
}

function activityResult(c, s) {
  if (!c.result) {
    return c.reference_baseline
      ? '<span class="act-result">Reference data</span>'
      : '<span class="act-result">No result</span>';
  }
  const out = [];
  if (s.dist === 1) {
    out.push('<span class="act-result is-alert">Response pattern changed</span>');
  }
  if (s.guardrail) {
    out.push('<span class="act-result is-alert">Too many responses failed</span>');
  } else if (s.availability) {
    out.push('<span class="act-result is-warn">Fewer responses succeeded</span>');
  }
  if (!out.length) {
    out.push('<span class="act-result">No significant change</span>');
  }
  return out.join("");
}

function renderActivity() {
  const el = $("activity-list");
  const count = $("activity-count");
  if (!el) return;
  if (state.failed.has("cycles")) {
    if (count) count.textContent = "";
    el.innerHTML = '<p class="empty-state">Recent cycles unavailable.</p>';
    return;
  }
  const all = [...(state.cycles || [])]
    .sort((a, b) => Number(b.cycle_number) - Number(a.cycle_number));
  const rows = all.slice(0, 5);
  if (count) {
    count.textContent = rows.length
      ? `${rows.length} of ${all.length} ${all.length === 1 ? "cycle" : "cycles"}`
      : "";
  }
  if (!rows.length) {
    el.innerHTML = '<p class="empty-state">No monitoring cycles recorded yet.</p>';
    return;
  }
  const body = rows.map((c) => {
    const s = cycleSignals(c.result);
    const [statusText, statusCls] = cycleStatus(c);
    const probes = Number(c.probe_count || 0);
    const empty = Number(c.empty_count || 0);
    const failed = Number(c.failed_count || 0);
    const notes = [];
    if (empty) {
      notes.push(`<span class="act-note">${empty} empty ${
        empty === 1 ? "response" : "responses"}</span>`);
    }
    if (failed) {
      notes.push(`<span class="act-note is-alert">${failed} failed</span>`);
    }
    const probeCell = `<span class="act-count">${probes}</span>`
      + (notes.length ? `<span class="act-notes">${notes.join("")}</span>` : "");
    return `
      <tr data-testid="activity-row" title="Cycle ID ${escapeHtml(c.cycle_id || "unknown")}">
        <td class="act-cycle" data-label="Cycle">#${escapeHtml(c.cycle_number)}</td>
        <td class="act-time" data-label="Date">${fmtTime(c.ended_at || c.started_at || c.timestamp)}</td>
        <td class="num act-requests" data-label="Requests">${probeCell}</td>
        <td class="act-status-cell" data-label="Status"><span class="${statusCls}">${statusText}</span></td>
        <td class="act-outcome" data-label="Result">${activityResult(c, s)}</td>
      </tr>`;
  }).join("");
  el.innerHTML = `
    <table class="activity-table">
      <thead>
        <tr>
          <th>Cycle</th>
          <th>Date</th>
          <th class="num">Requests</th>
          <th>Status</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>`;
}

/* ---------- alerts ---------- */

function renderAlerts() {
  const el = $("alerts-list");
  const count = $("alert-count");
  if (!el) return;
  if (state.failed.has("alerts")) {
    if (count) count.textContent = "";
    el.innerHTML = '<p class="empty-state">Alerts unavailable.</p>';
    return;
  }
  const rows = (state.alerts || []).slice(0, 3);
  if (count) count.textContent = rows.length
    ? `${rows.length} recent` : "";
  if (!rows.length) {
    el.innerHTML = '<p class="empty-state">No alerts.</p>';
    return;
  }
  el.innerHTML = rows.map((a) => {
    const detectors = Array.isArray(a.flagged_tests)
      ? a.flagged_tests.join(",")
      : (Array.isArray(a.bh_significant_tests) ? a.bh_significant_tests.join(",") : "");
    return alertCopy(a).map((item) => `
      <div class="alert-item alert-${item.kind}"
           data-alert-type="${escapeHtml(alertType(a))}"
           data-severity="${escapeHtml(a.severity || "")}"
           data-detectors="${escapeHtml(detectors)}">
        <span class="alert-time">${fmtTime(a.timestamp)}</span>
        <span class="alert-type">${escapeHtml(item.title)}</span>
        <span class="alert-msg">${escapeHtml(item.text)}</span>
      </div>`).join("");
  }).join("");
}

/* ---------- detector history ---------- */

/* Actual evaluated cycles only, latest 12, chronological. One cycle is a
   point, two are a connected pair, three or more form a trend: the graph
   never hides itself waiting for an arbitrary minimum. Null adjusted
   p-values break the line, so a gap means "not measured", never p = 0. */
const HISTORY_POINTS = 12;

function historyRows() {
  return (state.cycles || [])
    .filter((c) => c && c.result)
    .sort((a, b) => Number(a.cycle_number) - Number(b.cycle_number))
    .slice(-HISTORY_POINTS)
    .map((c) => ({
      ...c.result,
      cycle_number: c.cycle_number,
      cycle_id: c.cycle_id,
      cycle_time: c.ended_at || c.started_at || c.timestamp,
    }));
}

function renderHistory() {
  const el = $("history-chart");
  if (!el) return;
  const note = $("history-note");
  const alpha = state.config && state.config.alpha != null
    ? Number(state.config.alpha) : 0.05;
  if (state.failed.has("cycles")) {
    if (note) note.textContent = "";
    el.innerHTML = '<p class="empty-state">Detector history unavailable.</p>';
    return;
  }
  const rows = historyRows();
  if (!rows.length) {
    if (note) note.textContent = "Building baseline.";
    el.innerHTML = '<p class="chart-empty chart-empty-compact">'
      + 'Building baseline. Results appear here after the first completed check.'
      + '</p>';
    return;
  }
  if (note) {
    note.textContent = `${rows.length} checked ${rows.length === 1 ? "cycle" : "cycles"}. `
      + "Adjusted p-values shown.";
  }
  el.innerHTML = svgHistory(rows, alpha);
}

/* Distinct marker shapes keep the three detectors apart even in greyscale:
   KS circle, availability square, paired MMD triangle. */
function historyMarker(shape, x, y, color, size, title, flagged) {
  const stroke = flagged ? "#DC2626" : color;
  const strokeWidth = flagged ? "1.6" : "0";
  const common = `fill="${color}" stroke="${stroke}" stroke-width="${strokeWidth}"`;
  let mark;
  if (shape === "square") {
    const d = size * 1.7;
    mark = `<rect x="${(x - d / 2).toFixed(1)}" y="${(y - d / 2).toFixed(1)}" `
      + `width="${d.toFixed(1)}" height="${d.toFixed(1)}" rx="0.8" ${common}>`;
  } else if (shape === "triangle") {
    const d = size * 1.9;
    mark = `<polygon points="${x.toFixed(1)},${(y - d / 2).toFixed(1)} `
      + `${(x - d / 2).toFixed(1)},${(y + d / 2).toFixed(1)} `
      + `${(x + d / 2).toFixed(1)},${(y + d / 2).toFixed(1)}" ${common}>`;
  } else {
    mark = `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${size}" ${common}>`;
  }
  return mark + `<title>${escapeHtml(title)}</title>`
    + (shape === "circle" ? "</circle>" : shape === "square" ? "</rect>" : "</polygon>");
}

function svgHistory(rows, alpha) {
  // A single observation gets a shorter canvas: a full-height plot with one
  // point reads as a broken chart rather than limited history.
  const h = rows.length <= 1 ? 132 : 190;
  const w = 560, m = { top: 12, right: 14, bottom: 34, left: 34 };
  const iw = w - m.left - m.right;
  const ih = h - m.top - m.bottom;
  const n = rows.length;
  const last = n - 1;
  /* A single cycle sits in the middle of the plot rather than on the left
     edge, which would imply missing history to its left. */
  const sx = (i) => n === 1
    ? m.left + iw / 2
    : m.left + (i / last) * iw;
  const sy = (v) => m.top + ih - Math.max(0, Math.min(1, v)) * ih;
  const series = [
    { key: "ks_adjusted_pvalue", sig: "ks_significant", name: "KS",
      shape: "circle", color: "#4F46E5" },
    { key: "proportion_z_adjusted_pvalue", sig: "proportion_z_significant",
      name: "Availability", shape: "square", color: "#0F766E" },
    { key: "mmd_adjusted_pvalue", sig: "mmd_significant", name: "Paired MMD",
      shape: "triangle", color: "#A1A1AA" },
  ];
  const parts = [];

  for (const v of [0, 0.25, 0.5, 0.75, 1]) {
    const y = sy(v);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${w - m.right}" y2="${y}" stroke="#F1F1F3"/>`);
    parts.push(`<text x="${m.left - 6}" y="${y + 3}" font-size="10" fill="#71717A" text-anchor="end">${v}</text>`);
  }
  const ty = sy(alpha);
  parts.push(`<line x1="${m.left}" y1="${ty}" x2="${w - m.right}" y2="${ty}" stroke="#DC2626" stroke-width="1" stroke-dasharray="4 3"/>`);
  parts.push(`<text x="${w - m.right}" y="${ty - 5}" font-size="10" fill="#DC2626" text-anchor="end">alpha ${alpha}</text>`);

  /* The latest cycle is a distinct column, not another anonymous point. */
  if (n > 1) {
    parts.push(`<line x1="${sx(last).toFixed(1)}" y1="${m.top}" x2="${sx(last).toFixed(1)}" y2="${h - m.bottom}" stroke="#E4E4E7" stroke-dasharray="2 3"/>`);
  }

  const stride = Math.max(1, Math.ceil(n / 6));
  rows.forEach((r, i) => {
    if (n > 1 && i % stride !== 0 && i !== last) return;
    const x = sx(i);
    parts.push(`<line x1="${x}" y1="${h - m.bottom}" x2="${x}" y2="${h - m.bottom + 4}" stroke="#D4D4D8"/>`);
    parts.push(`<text x="${x}" y="${h - m.bottom + 15}" font-size="10" fill="#71717A" text-anchor="middle">#${escapeHtml(r.cycle_number)}</text>`);
  });
  parts.push(`<text x="${m.left + iw / 2}" y="${h - 4}" font-size="10" fill="#71717A" text-anchor="middle">checked cycle</text>`);

  for (const s of series) {
    let path = "";
    let open = false;
    rows.forEach((r, i) => {
      const v = r[s.key];
      if (v == null) { open = false; return; }
      path += `${open ? "L" : "M"}${sx(i).toFixed(1)} ${sy(Number(v)).toFixed(1)} `;
      open = true;
    });
    if (path && n > 1) {
      parts.push(`<path d="${path.trim()}" fill="none" stroke="${s.color}" stroke-width="1.6"/>`);
    }
    rows.forEach((r, i) => {
      const v = r[s.key];
      if (v == null) return;
      const flagged = Number(r[s.sig]) === 1;
      const title = `Cycle ${r.cycle_number}, ${fmtTime(r.cycle_time)}, `
        + `${s.name} adjusted p ${fmtP(Number(v))}${flagged ? " (flagged)" : ""}`;
      if (i === last && n > 1 && s.shape === "circle") {
        parts.push(`<circle cx="${sx(i).toFixed(1)}" cy="${sy(Number(v)).toFixed(1)}" `
          + `r="5.4" fill="none" stroke="${s.color}" stroke-width="1" opacity="0.4"/>`);
      }
      const size = i === last ? 3.6 : 2.8;
      parts.push(historyMarker(s.shape, sx(i), sy(Number(v)), s.color,
        size, title, flagged));
    });
  }

  const legend = `<div class="chart-legend">`
    + series.map((s) => `<span class="legend-item">`
      + `<span class="legend-marker mk-${s.shape}" style="color:${s.color}"></span>`
      + `${s.name}</span>`).join("")
    + (n > 1
      ? `<span class="legend-item"><span class="legend-latest"></span>latest cycle</span>`
      : "")
    + `<span class="legend-item"><span class="legend-dash"></span>${alpha} threshold</span>`
    + `</div>`;

  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img"
    aria-label="Adjusted p-values per checked cycle">${parts.join("")}</svg>${legend}`;
}

/* ---------- data loading ---------- */

let wakePromise = null;

/* Each data endpoint is its own task: fetch, store, and render exactly the
   panels that depend on it. A failed endpoint records itself in
   state.failed so its panel renders an explicit unavailable state. */
const DATA_LOADERS = [
  {
    key: "config", url: "/config",
    render: () => { renderConfig(); renderRunNote(); renderHistory(); },
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
  { key: "cycles", url: "/cycles?limit=24",
    render: () => { renderActivity(); renderHistory(); } },
  /* Alerts decorate activity rows and fill the alerts panel. */
  { key: "alerts", url: "/alerts?limit=10",
    render: () => { renderActivity(); renderAlerts(); } },
  { key: "comparison", url: "/drift/comparison", render: renderComparison },
  { key: "categories", url: "/drift/categories", render: renderCategories },
];

async function loadOne(def) {
  try {
    state[def.key] = await fetchJson(def.url);
    state.failed.delete(def.key);
    state.dataOk = true;
    if (def.key === "cycles") state.cyclesLoaded = true;
  } catch (err) {
    state[def.key] = (def.key === "cycles" || def.key === "alerts") ? [] : null;
    state.failed.add(def.key);
  }
  state.dataFailed = state.failed.size > 0;
  try {
    def.render();
  } catch (err) { /* a renderer must never take the page down */ }
  if (state[def.key] != null && !state.awake) {
    state.awake = true;
  }
  /* Data evidence changes the service state even when /health/live failed. */
  renderHealthPill();
}

/* The backend caches a real provider ping for PROVIDER_HEALTH_TTL_SECONDS.
   Use that value when /config supplies it; otherwise keep the documented
   default in sync. Never let this frontend interval exceed the backend TTL
   by five minutes, or an early `unchecked` result can outlive a healthy
   provider. */
function healthRefreshMs() {
  const ttl = state.config && state.config.provider_health_ttl_seconds;
  if (typeof ttl === "number" && Number.isFinite(ttl) && ttl > 0) {
    return Math.max(1000, Math.round(ttl * 1000));
  }
  return DEFAULT_HEALTH_REFRESH_MS;
}

/* Only a decisive provider verdict may be reused. `unchecked`, an
   unrecognised value, or a failed readiness request must be retried on the
   next refresh instead of being frozen in application state. */
function providerStatusSettled(health) {
  const status = health && health.provider_status;
  return status === "ok" || status === "unreachable"
    || status === "not_configured";
}

/* Every loadHealth() invocation gets a sequence number. Startup, the TTL
   interval and run completion can overlap; only the newest invocation may
   write state. This is what stops a slow failure from arriving after a newer
   success and turning a healthy Gemini back into "Unknown". */
let healthRequestSeq = 0;
let healthRetryTimer = null;

function clearHealthRetry() {
  if (healthRetryTimer !== null) {
    clearTimeout(healthRetryTimer);
    healthRetryTimer = null;
  }
}

function scheduleHealthRetry() {
  if (healthRetryTimer !== null) return;
  healthRetryTimer = setTimeout(() => {
    healthRetryTimer = null;
    loadHealth();
  }, HEALTH_RETRY_MS);
}

/* Readiness is separate from liveness and never blocks the first render.
   The provider ping costs quota, so a fresh settled answer is reused for the
   backend TTL. A failed or unresolved readiness call is retried once quickly
   and then by the normal TTL interval, instead of claiming the provider is
   down forever. */
async function loadHealth() {
  const requestSeq = ++healthRequestSeq;
  const now = Date.now();
  if (state.health && !state.healthError
      && providerStatusSettled(state.health)
      && now - state.healthCheckedAt < healthRefreshMs()) return false;
  try {
    // Readiness must be fresh: never let a browser heuristic reuse an old
    // dependency snapshot. The API also sends Cache-Control: no-store.
    const payload = await fetchJson("/health", { cache: "no-store" });
    // A superseded request must not rewrite a newer verdict, even if that
    // newer verdict was served from the TTL cache and made no HTTP request.
    if (requestSeq !== healthRequestSeq) return false;
    clearHealthRetry();
    state.health = payload;
    state.healthError = null;
    // Stamp the response time, not the request start: a slow ping must not
    // immediately look stale and trigger a second provider call.
    state.healthCheckedAt = Date.now();
    // Surface the API's dependency detail without echoing secrets. The
    // request's own HTTP status lives on state.healthError, not here.
    const d = payload && payload.diagnostics;
    if (d && (d.timeout || d.error_detail)) {
      console.warn("readiness diagnostics", {
        timeout: d.timeout, dbReady: d.db_ready,
        providerReady: d.provider_ready, detail: d.error_detail,
      });
    }
  } catch (err) {
    if (requestSeq !== healthRequestSeq) return false;
    clearHealthRetry();
    state.health = null;
    state.healthError = {
      httpStatus: err.status || null,
      timeout: Boolean(err.isTimeout || (err.name === "AbortError")),
      kind: err.isTimeout ? "timeout"
        : (err.status ? `HTTP ${err.status}` : (err.name || "network")),
      detail: String(err.detail || err.message || err).slice(0, 240),
    };
    state.healthCheckedAt = Date.now();
    // A failed readiness fetch is not proof the database is down; other
    // DB-backed endpoints remain the authority for the DB pill.
    console.warn("readiness fetch failed", state.healthError);
    scheduleHealthRetry();
  }
  renderStatusGroup();
  renderRunNote();
  renderModelLabel();
  /* A deployment without provider credentials has no target_model on
     /config; readiness supplies the model name, so refresh the label. */
  if (state.config && !state.config.target_model) renderConfig();
  return true;
}

function loadAll() {
  return Promise.allSettled(DATA_LOADERS.map((def) => loadOne(def)));
}

/* A run ending (completed, failed, lost) changes the data panels, but the
   provider pill reads only /health. Refresh both: unresolved or failed
   readiness must not survive a finished cycle just because loadAll() re-rendered
   the stale state. loadHealth() still honours the backend TTL, so a fresh
   settled result costs no extra provider ping. */
async function refreshAfterRun() {
  await Promise.all([loadAll(), loadHealth()]);
}

/* Fast liveness probe. A failure on its own is not proof that the service
   is down: the data endpoints are the stronger evidence, and apiState()
   resolves the two. */
async function loadLive() {
  try {
    state.live = await fetchJson("/health/live", { cache: "no-store" });
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
        banner("Still connecting. Data panels appear as endpoints answer.");
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
      el.textContent = "n/a";
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

/* ---------- demo tab ---------- */

/* The Demo view is intentionally isolated from live state. It fetches one
   committed artifact, validates it, and renders from that object only. A
   missing or malformed artifact becomes an explicit state; no panel falls
   back to live data or to an invented verdict. */

const DEMO_DETECTORS = [
  { key: "length", name: "Length", shape: "circle", color: "#4F46E5",
    method: "KS" },
  { key: "availability", name: "Availability", shape: "square", color: "#0F766E",
    method: "Fisher / z", quality: true },
  { key: "semantic", name: "Semantic", shape: "triangle", color: "#A1A1AA",
    method: "Paired MMD" },
];

function validateDemoArtifact(artifact) {
  if (!artifact || typeof artifact !== "object") {
    throw new Error("artifact is not an object");
  }
  if (artifact.schema_version !== 1) throw new Error("unsupported schema version");
  if (!artifact.methodology || typeof artifact.methodology !== "object") {
    throw new Error("methodology missing");
  }
  if (!artifact.configuration || typeof artifact.configuration !== "object") {
    throw new Error("configuration missing");
  }
  if (!Array.isArray(artifact.scenarios) || artifact.scenarios.length === 0) {
    throw new Error("no scenarios");
  }
  for (const scenario of artifact.scenarios) {
    if (!scenario || typeof scenario.id !== "string"
        || !Array.isArray(scenario.cycles) || scenario.cycles.length === 0) {
      throw new Error("malformed scenario");
    }
    for (const cycle of scenario.cycles) {
      if (!cycle || !Number.isFinite(Number(cycle.cycle_number))) {
        throw new Error("malformed cycle");
      }
    }
  }
  return true;
}

async function loadDemoArtifact() {
  if (demo.loaded || demo.loading) return;
  demo.loading = true;
  try {
    const artifact = await fetchJson(DEMO_ARTIFACT_URL, { cache: "no-store" });
    validateDemoArtifact(artifact);
    demo.artifact = artifact;
    demo.loaded = true;
    demo.error = null;
    const first = artifact.scenarios[0];
    demo.scenarioId = first.id;
    demo.cycleNumber = first.cycles[first.cycles.length - 1].cycle_number;
  } catch (err) {
    demo.loaded = false;
    demo.artifact = null;
    const reason = err && err.status ? `HTTP ${err.status}`
      : (err && err.message ? String(err.message).slice(0, 90) : "unknown error");
    demo.error = `Demo artifact unavailable (${reason}). `
      + "Monitor and Evaluation are unaffected.";
  } finally {
    demo.loading = false;
    renderDemo();
  }
}

function demoScenario() {
  if (!demo.artifact || !Array.isArray(demo.artifact.scenarios)) return null;
  return demo.artifact.scenarios.find((s) => s && s.id === demo.scenarioId) || null;
}

function demoCycle() {
  const scenario = demoScenario();
  if (!scenario) return null;
  return scenario.cycles.find(
    (c) => Number(c.cycle_number) === Number(demo.cycleNumber)) || null;
}

function initDemoEvents() {
  $("demo-scenario-buttons").addEventListener("click", (event) => {
    const button = event && event.target && event.target.closest
      ? event.target.closest("button[data-scenario]") : null;
    if (button && button.dataset && button.dataset.scenario) {
      selectDemoScenario(button.dataset.scenario);
    }
  });
  $("demo-cycle-list").addEventListener("click", (event) => {
    const button = event && event.target && event.target.closest
      ? event.target.closest("button[data-cycle]") : null;
    if (button && button.dataset && button.dataset.cycle) {
      selectDemoCycle(button.dataset.cycle);
    }
  });
}

function selectDemoScenario(id) {
  const scenario = (demo.artifact && demo.artifact.scenarios || [])
    .find((s) => s && s.id === id);
  if (!scenario) return;
  demo.scenarioId = id;
  const last = scenario.cycles[scenario.cycles.length - 1];
  demo.cycleNumber = last.cycle_number;
  renderDemo();
}

function selectDemoCycle(number) {
  const scenario = demoScenario();
  if (!scenario) return;
  const value = Number(number);
  if (!scenario.cycles.some((c) => Number(c.cycle_number) === value)) return;
  demo.cycleNumber = value;
  renderDemo();
}

function demoRenderSafe(id, label, fn) {
  try {
    fn();
  } catch (err) {
    const el = $(id);
    if (el) {
      el.innerHTML = `<p class="empty-state">${escapeHtml(label)} unavailable.</p>`;
    }
  }
}

function renderDemo() {
  const errorBox = $("demo-error");
  const body = $("demo-body");
  if (!errorBox || !body) return;
  if (!demo.artifact || demo.error) {
    errorBox.hidden = false;
    errorBox.textContent = demo.error || "Demo artifact unavailable.";
    body.hidden = true;
    return;
  }
  const scenario = demoScenario();
  if (!scenario) {
    demo.error = "Demo artifact has no selectable scenario.";
    renderDemo();
    return;
  }
  errorBox.hidden = true;
  errorBox.textContent = "";
  body.hidden = false;

  if (!scenario.cycles.some((c) => Number(c.cycle_number) === Number(demo.cycleNumber))) {
    const last = scenario.cycles[scenario.cycles.length - 1];
    demo.cycleNumber = last.cycle_number;
  }
  renderDemoScenarios(scenario);
  renderDemoCycles(scenario);
  demoRenderSafe("demo-pvalues-chart", "Detector trajectory", renderDemoPValueChart);
  demoRenderSafe("demo-length-chart", "Response length chart", renderDemoLengthChart);
  demoRenderSafe("demo-detector-rows", "Detector results", renderDemoDetectors);
  demoRenderSafe("demo-insight", "Cycle decision", renderDemoDecision);
  demoRenderSafe("demo-alert", "Alert explanation", renderDemoAlert);
  demoRenderSafe("demo-samples", "Response samples", renderDemoSamples);
  renderDemoMethodology();
}

function demoVerdictCopy(value) {
  if (value === "No shift") return "No significant change in response patterns.";
  if (value === "Shift detected") return "Response pattern changed.";
  return value || "";
}

function demoScenarioLabel(s) {
  if (s.id === "stable") return "Stable responses";
  if (s.id === "injected-drift") return "Changed responses";
  return s.label || humanizeKey(s.id || "scenario");
}

function demoScenarioCopy(scenario) {
  if (scenario.id === "stable") {
    return "Three cycles build the baseline. The checked cycles show no significant "
      + "change in response patterns.";
  }
  if (scenario.id === "injected-drift") {
    return "Three cycles build the baseline. From cycle 4, responses change. "
      + "The first change stays silent; an alert is recorded when it repeats.";
  }
  return `${scenario.description || ""} ${demoVerdictCopy((scenario.summary || {}).verdict)}`.trim();
}

function renderDemoScenarios(scenario) {
  const cfg = demo.artifact.configuration || {};
  $("demo-scenario-meta").textContent =
    `${cfg.prompts_per_cycle || "?"} synthetic responses per check`;
  $("demo-scenario-desc").textContent = demoScenarioCopy(scenario);
  $("demo-scenario-buttons").innerHTML = demo.artifact.scenarios.map((s) => {
    const active = s.id === scenario.id;
    const icon = /drift/i.test(s.id) ? "alert" : "check";
    return `<button class="demo-segment${active ? " selected" : ""}" type="button" `
      + `data-scenario="${escapeHtml(s.id)}" aria-pressed="${active}" `
      + `data-testid="demo-scenario-btn">${iconSvg(icon)}`
      + `${escapeHtml(demoScenarioLabel(s))}</button>`;
  }).join("");
}

function renderDemoCycles(scenario) {
  const list = $("demo-cycle-list");
  const evaluated = scenario.cycles.filter((c) => c.tests_run).length;
  $("demo-cycle-meta").textContent =
    `${scenario.cycles.length} cycles, ${evaluated} checked`;
  list.innerHTML = scenario.cycles.map((cycle) => {
    const active = Number(cycle.cycle_number) === Number(demo.cycleNumber);
    const result = cycle.results || {};
    const flagged = Boolean(result.distribution_shift_detected);
    const alerts = (cycle.alerts || []).length;
    const state = !cycle.tests_run ? "reference"
      : (alerts ? "alert" : (flagged ? "flagged" : "clear"));
    const label = !cycle.tests_run ? "Baseline" : "Complete";
    const icon = state === "reference" ? "clock"
      : (state === "clear" ? "check" : "alert");
    return `<button class="demo-cycle-card ${state}${active ? " selected" : ""}" `
      + `type="button" data-cycle="${escapeHtml(cycle.cycle_number)}" `
      + `aria-pressed="${active}" data-testid="demo-cycle" `
      + `title="Cycle ${escapeHtml(cycle.cycle_number)}, ${escapeHtml(label)}">`
      + iconSvg(icon)
      + `<span class="demo-cycle-num">${escapeHtml(cycle.cycle_number)}</span>`
      + `<span class="demo-cycle-label">${escapeHtml(humanizeKey(cycle.phase || "cycle"))}</span>`
      + `<span class="demo-cycle-state">${escapeHtml(label)}</span>`
      + `</button>`;
  }).join("");
}

function demoSelectedMarker(shape, x, y, color, title) {
  const common = `fill="${color}" stroke="${color}" stroke-width="0"`;
  let mark;
  if (shape === "square") {
    mark = `<rect x="${(x - 3.2).toFixed(1)}" y="${(y - 3.2).toFixed(1)}" `
      + `width="6.4" height="6.4" ${common}>`;
  } else if (shape === "triangle") {
    mark = `<polygon points="${x.toFixed(1)},${(y - 4).toFixed(1)} `
      + `${(x - 3.8).toFixed(1)},${(y + 3.5).toFixed(1)} `
      + `${(x + 3.8).toFixed(1)},${(y + 3.5).toFixed(1)}" ${common}>`;
  } else {
    mark = `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.4" ${common}>`;
  }
  return mark + `<title>${escapeHtml(title)}</title></`
    + (shape === "square" ? "rect" : shape === "triangle" ? "polygon" : "circle") + `>`;
}

function renderDemoPValueChart() {
  const scenario = demoScenario();
  const cycles = (scenario && scenario.cycles) || [];
  const cfg = (demo.artifact && demo.artifact.configuration) || {};
  const alpha = Number(cfg.alpha) || 0.05;
  const w = 560, h = 178, m = { top: 12, right: 14, bottom: 32, left: 36 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const last = Math.max(1, cycles.length - 1);
  const sx = (i) => m.left + (cycles.length === 1 ? iw / 2 : (i / last) * iw);
  const sy = (v) => m.top + ih - Math.max(0, Math.min(1, v)) * ih;
  const parts = [];

  for (const value of [0, 0.25, 0.5, 0.75, 1]) {
    const y = sy(value);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${w - m.right}" y2="${y}" stroke="#F1F1F3"/>`);
    parts.push(`<text x="${m.left - 5}" y="${y + 3}" font-size="10" fill="#71717A" `
      + `text-anchor="end">${value}</text>`);
  }
  const alphaY = sy(alpha);
  parts.push(`<line x1="${m.left}" y1="${alphaY}" x2="${w - m.right}" y2="${alphaY}" `
    + `stroke="#DC2626" stroke-width="1" stroke-dasharray="4 3"/>`);
  parts.push(`<text x="${w - m.right}" y="${alphaY - 4}" font-size="10" fill="#DC2626" `
    + `text-anchor="end">alpha ${alpha}</text>`);

  const selectedIndex = cycles.findIndex(
    (c) => Number(c.cycle_number) === Number(demo.cycleNumber));
  if (selectedIndex >= 0 && cycles.length > 1) {
    const x = sx(selectedIndex).toFixed(1);
    parts.push(`<line x1="${x}" y1="${m.top}" x2="${x}" y2="${h - m.bottom}" `
      + `stroke="#A1A1AA" stroke-width="1" stroke-dasharray="2 3"/>`);
  }

  cycles.forEach((cycle, i) => {
    const x = sx(i);
    parts.push(`<line x1="${x}" y1="${h - m.bottom}" x2="${x}" `
      + `y2="${h - m.bottom + 4}" stroke="#D4D4D8"/>`);
    parts.push(`<text x="${x}" y="${h - m.bottom + 14}" font-size="10" fill="#71717A" `
      + `text-anchor="middle">#${escapeHtml(cycle.cycle_number)}</text>`);
  });

  for (const detector of DEMO_DETECTORS) {
    let path = "";
    let open = false;
    cycles.forEach((cycle, i) => {
      const row = ((cycle.results || {}).detectors || {})[detector.key];
      const value = row ? row.adjusted_p : null;
      if (value == null) { open = false; return; }
      path += `${open ? "L" : "M"}${sx(i).toFixed(1)} ${sy(Number(value)).toFixed(1)} `;
      open = true;
    });
    if (path && cycles.length > 1) {
      parts.push(`<path d="${path.trim()}" fill="none" stroke="${detector.color}" `
        + `stroke-width="1.5"/>`);
    }
    cycles.forEach((cycle, i) => {
      const row = ((cycle.results || {}).detectors || {})[detector.key];
      const value = row ? row.adjusted_p : null;
      if (value == null) return;
      const title = `Cycle ${cycle.cycle_number}, ${detector.name} adjusted p `
        + `${fmtP(Number(value))}${row.significant ? " (flagged)" : ""}`;
      parts.push(demoSelectedMarker(detector.shape, sx(i), sy(Number(value)),
        detector.color, title));
    });
  }
  parts.push(`<text x="${m.left + iw / 2}" y="${h - 3}" font-size="10" fill="#71717A" `
    + `text-anchor="middle">cycle</text>`);

  const legend = `<div class="chart-legend">`
    + DEMO_DETECTORS.map((d) => `<span class="legend-item">`
      + `<span class="legend-marker mk-${d.shape}" style="color:${d.color}"></span>`
      + `${d.name}</span>`).join("")
    + `</div>`;
  $("demo-pvalues-note").textContent = "Dotted guide: selected cycle.";
  $("demo-pvalues-chart").innerHTML = `<svg viewBox="0 0 ${w} ${h}" class="chart" `
    + `role="img" aria-label="Adjusted p-values by demo cycle">${parts.join("")}</svg>${legend}`;
}

function renderDemoLengthChart() {
  const scenario = demoScenario();
  const cycles = (scenario && scenario.cycles) || [];
  const values = cycles.map((c) => Number(c.mean_response_length))
    .filter((v) => Number.isFinite(v));
  if (!values.length) {
    $("demo-length-chart").innerHTML = '<p class="empty-state">No response lengths recorded.</p>';
    $("demo-length-note").textContent = "";
    return;
  }
  const min = Math.min(...values), max = Math.max(...values);
  const pad = max === min ? Math.max(10, Math.abs(max) * 0.1) : (max - min) * 0.15;
  const low = Math.max(0, min - pad), high = max + pad;
  const w = 560, h = 150, m = { top: 12, right: 14, bottom: 30, left: 42 };
  const iw = w - m.left - m.right, ih = h - m.top - m.bottom;
  const slot = iw / Math.max(1, cycles.length);
  const barWidth = Math.max(12, Math.min(38, slot * 0.56));
  const sy = (v) => m.top + ih - ((v - low) / (high - low)) * ih;
  const parts = [];
  for (let i = 0; i <= 2; i++) {
    const value = low + ((high - low) * i / 2);
    const y = sy(value);
    parts.push(`<line x1="${m.left}" y1="${y}" x2="${w - m.right}" y2="${y}" stroke="#F1F1F3"/>`);
    parts.push(`<text x="${m.left - 5}" y="${y + 3}" font-size="10" fill="#71717A" `
      + `text-anchor="end">${Math.round(value)}</text>`);
  }
  cycles.forEach((cycle, i) => {
    const value = Number(cycle.mean_response_length);
    if (!Number.isFinite(value)) return;
    const x = m.left + slot * (i + 0.5);
    const y = sy(value);
    const selected = Number(cycle.cycle_number) === Number(demo.cycleNumber);
    const color = selected ? "#4F46E5" : "#C7C7D1";
    parts.push(`<rect x="${(x - barWidth / 2).toFixed(1)}" y="${y.toFixed(1)}" `
      + `width="${barWidth.toFixed(1)}" height="${(h - m.bottom - y).toFixed(1)}" `
      + `fill="${color}" rx="2"><title>Cycle ${escapeHtml(cycle.cycle_number)}, `
      + `${Math.round(value)} characters</title></rect>`);
    parts.push(`<text x="${x.toFixed(1)}" y="${h - m.bottom + 14}" font-size="10" `
      + `fill="#71717A" text-anchor="middle">#${escapeHtml(cycle.cycle_number)}</text>`);
  });
  parts.push(`<text x="${m.left + iw / 2}" y="${h - 3}" font-size="10" fill="#71717A" `
    + `text-anchor="middle">cycle</text>`);
  $("demo-length-note").textContent = "";
  $("demo-length-chart").innerHTML = `<svg viewBox="0 0 ${w} ${h}" class="chart" `
    + `role="img" aria-label="Mean response length by demo cycle">${parts.join("")}</svg>`
    + '<div class="chart-legend"><span class="legend-item">'
    + '<span class="legend-swatch" style="background:#4F46E5"></span>selected cycle</span></div>';
}

function renderDemoDetectors() {
  const cycle = demoCycle();
  const result = cycle && cycle.results;
  const detectors = (result && result.detectors) || {};
  $("demo-detector-meta").textContent = cycle
    ? `Cycle ${cycle.cycle_number}, ${cycle.tests_run ? "checked" : "baseline"}`
    : "No cycle selected";
  $("demo-detector-rows").innerHTML = DEMO_DETECTORS.map((d) => {
    const row = detectors[d.key];
    const raw = row ? row.raw_p : null;
    const adjusted = row ? row.adjusted_p : null;
    const sig = row && row.significant != null ? Number(row.significant) : null;
    const decision = decisionState(adjusted, sig, Boolean(d.quality));
    const method = (row && row.method)
      ? humanizeKey(row.method) : d.method;
    return `<tr data-testid="demo-detector-row">`
      + `<td><span class="detector-name">${escapeHtml(d.name)}</span>`
      + `<span class="detector-method">${escapeHtml(method)}</span></td>`
      + `<td class="num">${raw == null ? "n/a" : fmtP(Number(raw))}</td>`
      + `<td class="num">${adjusted == null ? "n/a" : fmtP(Number(adjusted))}</td>`
      + `<td><span class="${decision.cls}">${decision.mark}${decision.text}</span></td></tr>`;
  }).join("");
}

function renderDemoDecision() {
  const cycle = demoCycle();
  const insight = $("demo-insight");
  if (!cycle) {
    insight.className = "demo-insight";
    insight.innerHTML = "<strong>Select a cycle to see the decision</strong>"
      + "<p>Choose a cycle from the timeline.</p>";
    return;
  }
  const result = cycle.results || {};
  const detectors = result.detectors || {};
  /* Only KS and MMD speak to response patterns. A success-rate change is a
     separate response problem, as in the live view. */
  const distributionKeys = DEMO_DETECTORS.filter((d) => !d.quality).map((d) => d.key);
  const passed = distributionKeys.filter((key) => {
    const row = detectors[key];
    return row && Number(row.significant) === 1;
  }).length;
  const flagged = Boolean(result.distribution_shift_detected) || passed > 0;
  const availabilityFlag = Number(detectors.availability
    && detectors.availability.significant) === 1
    || Number(result.quality_change_detected) === 1;
  const guardrail = Number(result.overall_quality_degradation) === 1;
  const alerts = cycle.alerts || [];
  $("demo-cycle-tag").textContent =
    `Cycle ${cycle.cycle_number}, ${humanizeKey(cycle.adapter_mode || "unspecified mode")}`;
  $("demo-count-ref").textContent = result.reference_size == null
    ? "n/a" : Number(result.reference_size).toLocaleString();
  $("demo-count-cur").textContent = result.current_size == null
    ? "n/a" : Number(result.current_size).toLocaleString();
  const effects = result.effects || {};
  const setEffect = (id, value) => {
    const el = $(id);
    el.textContent = value;
    el.className = `effect-value${value === "n/a" ? " effect-missing" : ""}`;
  };
  setEffect("demo-eff-length", effects.length_shift_pct == null ? "n/a"
    : `${effects.length_shift_pct > 0 ? "+" : ""}${effects.length_shift_pct}%`);
  setEffect("demo-eff-centroid", effects.centroid_distance == null
    ? "n/a" : Number(effects.centroid_distance).toFixed(3));
  setEffect("demo-eff-paired", effects.paired_mmd_pvalue == null
    ? "n/a" : fmtP(Number(effects.paired_mmd_pvalue)));

  if (!cycle.tests_run) {
    insight.className = "demo-insight";
    insight.innerHTML = "<strong>Building baseline</strong>"
      + "<p>This cycle is part of the reference period. Checks begin at cycle "
      + `${escapeHtml((demoScenario() || {}).tests_start_cycle || "?")}.</p>`;
    return;
  }
  const alert = alerts[0];
  const isIssue = Boolean(alert) || flagged || availabilityFlag || guardrail;
  insight.className = `demo-insight ${isIssue ? "alert" : "clear"}`;
  const findings = [];
  if (alert) findings.push(...alertCopy(alert, cycle));
  else {
    if (guardrail) {
      findings.push({
        title: "Too many responses failed",
        text: "Too many responses failed during this check.",
      });
    }
    if (availabilityFlag) {
      findings.push({
        title: "Fewer responses succeeded",
        text: "Fewer responses succeeded than in the baseline.",
      });
    }
  }
  if (flagged && !findings.some((f) => f.title === "Response pattern changed")) {
    findings.push({
      title: "Response pattern changed",
      text: `${passed} of ${distributionKeys.length} response pattern checks flagged this cycle.`,
    });
  }
  if (!findings.length) {
    findings.push({
      title: "No significant change",
      text: "No significant change in response patterns. Responses look stable.",
    });
  }
  insight.innerHTML = `<div class="demo-insight-icon">`
    + `${iconSvg(isIssue ? "alert" : "check")}</div>`
    + `<div>${findings.map((f) => `<strong>${escapeHtml(f.title)}</strong>`
      + `<p>${escapeHtml(f.text)}</p>`).join("")}</div>`;
}

function renderDemoAlert() {
  const cycle = demoCycle();
  const cfg = (demo.artifact && demo.artifact.configuration) || {};
  const streak = cfg.min_consecutive_cycles == null ? 1
    : Number(cfg.min_consecutive_cycles);
  const policy = `The alert fires after ${streak} consecutive changed cycles.`;
  const alerts = (cycle && cycle.alerts) || [];
  const flagged = Boolean((cycle.results || {}).distribution_shift_detected);
  $("demo-alert-meta").textContent = cycle
    ? `${alerts.length} alert${alerts.length === 1 ? "" : "s"} for cycle ${cycle.cycle_number}`
    : "";
  /* The rule is shown as a compact sequence, not a paragraph of policy copy. */
  const steps = `<div class="policy-steps">`
    + `<span class="policy-step${flagged ? " done" : ""}">${iconSvg("radar")}Change</span>`
    + `<span class="policy-line" aria-hidden="true"></span>`
    + `<span class="policy-step${streak > 1 ? (alerts.length ? " fired" : " done") : ""}">`
    + `${iconSvg("clock")}Repeat ${streak} cycles</span>`
    + `<span class="policy-line" aria-hidden="true"></span>`
    + `<span class="policy-step${alerts.length ? " fired" : ""}">`
    + `${iconSvg("bell")}Alert</span></div>`
    + `<p class="demo-policy">${escapeHtml(policy)}</p>`;

  if (!cycle || !cycle.tests_run) {
    $("demo-alert").innerHTML =
      '<p class="empty-state">No alert during the baseline period.</p>' + steps;
    return;
  }
  if (alerts.length) {
    $("demo-alert").innerHTML = alerts.map((alert) => alertCopy(alert, cycle).map((item) => `
      <div class="demo-alert-item">
        <span class="demo-alert-type">${escapeHtml(item.title)}</span>
        <p>${escapeHtml(item.text)}</p>
      </div>`).join("")).join("") + steps;
    return;
  }
  $("demo-alert").innerHTML = (flagged
    ? '<p class="empty-state">No alert yet. The change has not repeated.</p>'
    : '<p class="empty-state">No change in this cycle, so no alert.</p>')
    + steps;
}

function demoResponse(sample, side) {
  if (sample && sample.redacted) return "(withheld: safety boundary prompt)";
  return respText(side);
}

function renderDemoSamples() {
  const cycle = demoCycle();
  const samples = (cycle && cycle.samples) || [];
  $("demo-sample-count").textContent = samples.length ? `${samples.length} prompts` : "";
  if (!samples.length) {
    $("demo-samples").innerHTML = '<p class="empty-state">No response samples for this cycle.</p>';
    return;
  }
  $("demo-samples").innerHTML = samples.map((sample, i) => {
    const reference = sample.reference || {};
    const current = sample.current || {};
    const refLength = reference.length == null ? "n/a" : `${reference.length} ch`;
    const curLength = current.length == null ? "n/a" : `${current.length} ch`;
    const delta = reference.length != null && current.length != null
      ? Number(current.length) - Number(reference.length) : null;
    const change = sample.changed
      ? `<span class="s-changed">changed</span>${delta == null ? "" : `
        <span class="s-delta">${delta > 0 ? "+" : ""}${delta} ch</span>`}`
      : '<span class="s-state">unchanged</span>';
    const label = sample.label || sample.prompt_id || "prompt";
    const side = (name, data, text) => `<div class="demo-sample-col">`
      + `<span class="demo-sample-side">${name}</span>`
      + `<span class="demo-sample-meta">${escapeHtml(text)} ${sampleValidity(data)}</span>`
      + `<code>${escapeHtml(demoResponse(sample, data))}</code></div>`;
    return `<details class="demo-sample-row" data-testid="demo-sample"${i === 0 ? " open" : ""}>`
      + `<summary title="Compare ${escapeHtml(label)}">`
      + `<span class="demo-sample-prompt"><span class="s-label">${escapeHtml(label)}</span>`
      + `${sample.category ? `<span class="s-cat">${escapeHtml(humanizeKey(sample.category))}</span>` : ""}`
      + `</span><span class="demo-sample-counts"><b>${escapeHtml(refLength)}</b> to `
      + `<b>${escapeHtml(curLength)}</b></span><span class="demo-sample-change">${change}</span>`
      + `<span class="demo-sample-chevron">${iconSvg("chevron-right")}</span></summary>`
      + `<div class="demo-sample-compare">`
      + side("Reference", reference, refLength)
      + side("Current", current, curLength)
      + `</div></details>`;
  }).join("");
}

function renderDemoMethodology() {
  $("demo-method-disclosure").textContent =
    "Every response shown here is synthetic. No live model, key, database or "
    + "monitoring data was used to produce them.";
  $("demo-method-pipeline").textContent =
    "These synthetic responses are processed by the real monitoring pipeline.";
  $("demo-method-limitation").textContent = [
    "This is a controlled demonstration, not evidence about a live model.",
    "Measured results are in the Evaluation view.",
  ].join(" ");
}

/* ---------- evaluation tab ---------- */

function setEvaluationError(message) {
  const error = $("evaluation-error");
  if (error) {
    error.hidden = !message;
    error.textContent = message || "";
  }
  if (message) {
    for (const id of ["ev-delay", "ev-fa", "ev-detect", "ev-overhead"]) {
      $(id).textContent = "n/a";
    }
    for (const id of [
      "eval-tradeoff-chart", "eval-tradeoff", "eval-power-chart",
      "eval-power", "eval-reliability", "ev-methodology",
    ]) {
      $(id).innerHTML = '<p class="empty-state">Evaluation results unavailable.</p>';
    }
  }
}

async function loadEvaluation() {
  loadProviderComparison();
  try {
    state.evaluation = await fetchJson("/evaluation");
  } catch (err) {
    /* Do not keep rendering a stale benchmark as if it were the current
       answer; the next tab visit must retry the failed endpoint. */
    state.evaluation = null;
    setEvaluationError("Evaluation results unavailable.");
    return;
  }
  setEvaluationError("");
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
    <td>${a.ok} / ${a.failed}</td><td>${a.p50 || "n/a"}</td>
    <td>${a.p95 || "n/a"}</td><td>${a.tokens || "n/a"}</td><td>${v}</td></tr>`;
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
      `This recorded comparison uses different providers. It does not measure `
      + `drift within one model. ${noFlag
        ? "No test flagged and no alert was recorded. "
        : "Some tests flagged; see the artifacts. "}`
      + `Paired p-value ${fmtP(f1.paired_mmd_pvalue)} over `
      + `${f1.paired_prompt_pairs} matched prompts. The models and providers also `
      + `differ: ${pc.shared.uncontrolled_differences.join("; ")}. Sources: `
      + `${pc.source_artifacts.join(", ")}.`;
  }
}

/* Numeric keys everywhere: the CSV reader returns delta values as strings
   such as "0.0", and looking those up with the number 1 would miss the key
   "1.0" entirely. Rounds to one decimal, the resolution the source uses. */
function powerLookup(rows) {
  const map = new Map();
  for (const r of rows) {
    const delta = Number(r && r.delta);
    const power = Number(r && r.power);
    if (!Number.isFinite(delta) || !Number.isFinite(power)) continue;
    map.set(Number(delta.toFixed(1)), power);
  }
  return map;
}

const RULE_LABELS = {
  single: "Single cycle",
  kofn: "k of n",
  persist2: "Two consecutive cycles",
  cusum: "CUSUM",
};

function policyLabel(rule) {
  return RULE_LABELS[String(rule || "").toLowerCase()] || humanizeKey(rule);
}

function plainTable(headers, rows) {
  return `
    <table class="plain-table"><thead><tr>
      ${headers.map((x) => `<th>${x}</th>`).join("")}
    </tr></thead><tbody>
      ${rows.map((r) => `<tr>${r.map((x) => `<td>${x}</td>`).join("")}</tr>`).join("")}
    </tbody></table>`;
}

/* Every evaluation block renders independently. A malformed or missing
   array in one block must not erase the metrics or the other charts: the
   failing block states that it is unavailable and the rest still draw. */
function evalSafe(id, label, fn) {
  try {
    fn();
  } catch (err) {
    const el = $(id);
    if (el) {
      el.innerHTML = `<p class="empty-state">${escapeHtml(label)} unavailable.</p>`;
    }
  }
}

function renderTradeoff(ev, h) {
  const rows = Array.isArray(ev.tradeoff) ? ev.tradeoff : [];
  evalSafe("eval-tradeoff-chart", "Trade-off chart", () => {
    const points = rows
      .map((r) => ({
        x: Number(r.mean_detection_delay_days),
        y: Number(r.false_alarms_per_100_days),
        label: policyLabel(r.rule),
        shipped: r.rule === h.default_rule,
      }))
      .filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y));
    $("eval-tradeoff-chart").innerHTML = svgScatter(points, {
      xLabel: "Mean detection delay (days)",
      yLabel: "False alarms per 100 quiet days",
    });
  });
  evalSafe("eval-tradeoff", "Trade-off table", () => {
    $("eval-tradeoff").innerHTML = rows.length ? plainTable(
      ["Alert policy", "Delay (days)", "Detection rate", "False alarms / 100 d"],
      rows.map((r) => [
        `${escapeHtml(policyLabel(r.rule))}${r.rule === h.default_rule ? " (shipped)" : ""}`,
        Number(r.mean_detection_delay_days).toFixed(2),
        Number(r.detection_rate_pct).toFixed(1) + "%",
        Number(r.false_alarms_per_100_days).toFixed(2),
      ])) : '<p class="empty-state">Trade-off data unavailable.</p>';
  });
}

function renderPower(ev) {
  const curves = ev.power_curves || {};
  const mmd = powerLookup(Array.isArray(curves.mmd_n10) ? curves.mmd_n10 : []);
  const ks = powerLookup(Array.isArray(curves.ks_n10) ? curves.ks_n10 : []);
  const deltas = Array.from(new Set([...mmd.keys(), ...ks.keys()]))
    .sort((a, b) => a - b);
  evalSafe("eval-power-chart", "Power chart", () => {
    $("eval-power-chart").innerHTML = svgLines(deltas, [
      { name: "MMD", color: "#4F46E5",
        values: deltas.map((d) => (mmd.has(d) ? mmd.get(d) : null)) },
      { name: "KS", color: "#71717A",
        values: deltas.map((d) => (ks.has(d) ? ks.get(d) : null)) },
    ], {
      xLabel: "Shift magnitude (sigma)",
      yLabel: "Detection power",
      yMax: 1,
    });
  });
  evalSafe("eval-power", "Power table", () => {
    $("eval-power").innerHTML = deltas.length ? plainTable(
      ["Shift (sigma)", "MMD power (n=10)", "KS power (n=10)"],
      deltas.map((d) => [
        d.toFixed(1),
        mmd.has(d) ? Math.round(100 * mmd.get(d)) + "%" : "n/a",
        ks.has(d) ? Math.round(100 * ks.get(d)) + "%" : "n/a",
      ])) : '<p class="empty-state">Power curve data unavailable.</p>';
  });
  return curves.trials_per_point;
}

function renderReliability(ev) {
  const conditions = Array.isArray(ev.reliability_conditions)
    ? ev.reliability_conditions : [];
  const condNames = {
    null: "Quiet (nothing changed)", drift_only: "Distribution shift only",
    invalid_only: "Quality regression only", both: "Shift and regression",
  };
  evalSafe("eval-reliability", "Reliability table", () => {
    const relRows = conditions.filter((r) =>
      ["drift_only", "invalid_only", "both"].includes(r.condition)
      && ["pooled_bh", "dist_bh", "validity"].includes(r.signal));
    const grouped = relRows.reduce((acc, r) => {
      acc[r.condition] = acc[r.condition] || {};
      acc[r.condition][r.signal] = r;
      return acc;
    }, {});
    $("eval-reliability").innerHTML = Object.keys(grouped).length ? plainTable(
      ["Simulated condition", "Pooled BH family", "Distribution-only family",
        "Validity family"],
      Object.keys(grouped).map((cond) => [
        condNames[cond] || cond,
        pct(grouped[cond].pooled_bh),
        pct(grouped[cond].dist_bh),
        pct(grouped[cond].validity),
      ])) : '<p class="empty-state">Reliability data unavailable.</p>';
  });
}

function renderEvaluation() {
  const ev = state.evaluation;
  const h = (ev && ev.headline) || {};
  const sigma = h.evaluated_shift_sigma;
  const setMetric = (id, value) => {
    const el = $(id);
    if (el) el.textContent = value;
  };
  setMetric("ev-delay", h.mean_detection_delay_days == null
    ? "n/a" : `${Number(h.mean_detection_delay_days).toFixed(2)} d`);
  setMetric("ev-fa", h.false_alarms_per_100_days == null
    ? "n/a" : Number(h.false_alarms_per_100_days).toFixed(2));
  setMetric("ev-detect", h.detection_rate_pct == null
    ? "n/a" : `${Number(h.detection_rate_pct).toFixed(0)}%`);
  setMetric("ev-overhead", h.overhead_p95_seconds == null
    ? "n/a" : `${Math.round(h.overhead_p95_seconds * 1000)} ms`);

  renderTradeoff(ev, h);
  const trials = renderPower(ev);
  renderReliability(ev);

  evalSafe("ev-methodology", "Methodology", () => {
    $("ev-methodology").innerHTML =
      `Results come from controlled simulations with predefined shifts and alert `
      + `rules (sources: <code>experiments/</code>, <code>results/</code>). Delay and `
      + `power describe the shipped decision rule, not live traffic. Detection at `
      + `${sigma} sigma is estimated from a limited number of trials`
      + `${trials ? ` (${trials} per power point)` : ""}, not a guarantee across all `
      + `shifts. Category-level localization is weak at these sample sizes. The hosted `
      + `provider comparison uses different models, so it cannot measure drift within `
      + `one model. Full methodology: <a href="https://github.com/chandresh080/llm-drift-monitor/blob/main/METRICS.md" target="_blank" rel="noopener">METRICS.md</a>.`;
  });
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
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img" `
    + `aria-label="Mean detection delay against false alarms by alert policy">`
    + `${parts.join("")}</svg>`
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
  return `<svg viewBox="0 0 ${w} ${h}" class="chart" role="img" `
    + `aria-label="Detection power by shift magnitude and detector">`
    + `${parts.join("")}</svg>`
    + legendHtml(series);
}

/* ---------- run a check ---------- */

let runInFlight = false;

/* Keep the icon when the label changes to Running… and back. */
function setRunButton(label) {
  const btn = $("run-btn");
  btn.innerHTML = iconSvg("play") + `<span>${escapeHtml(label)}</span>`;
}

/* Completion copy follows the stored result fields, so a quality problem is
   never reported as a clean distribution result. */
function completionNote(result) {
  if (!result || !result.tests_run) return "Check complete. Building baseline.";
  const sig = latestSignals(result);
  const bits = ["Check complete."];
  bits.push(sig.dist === 1 ? "Response pattern changed."
    : "No significant change in response patterns.");
  if (sig.guardrail) bits.push("Too many responses failed during this check.");
  else if (sig.qualityChange) {
    bits.push("Fewer responses succeeded than in the baseline.");
  }
  return bits.join(" ");
}

async function runCheck() {
  const blocked = runBlockedReason();
  if (blocked) {
    $("run-note").dataset.active = "1";
    renderRunNote();
    $("run-btn").disabled = true;
    return;
  }
  if (runInFlight) return;
  runInFlight = true;
  const btn = $("run-btn"), note = $("run-note");
  btn.disabled = true;
  setRunButton("Running…");
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
      if (err.status === 503) {
        note.textContent = err.detail
          ? err.detail
          : "New checks are disabled because persistent storage is unavailable.";
        return;
      }
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
    note.textContent = "Running check. This may take a few minutes.";

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
          await refreshAfterRun();
          return;
        }
        continue;
      }
      misses = 0;

      if (st.status === "lost") {
        note.textContent = st.error || "The instance recycled during this check; run again.";
        await refreshAfterRun();
        return;
      }

      if (st.status === "completed") {
        const r = st.result || {};
        await refreshAfterRun();
        note.textContent = completionNote(r);
        return;
      }
      if (st.status === "failed") {
        note.textContent = "That run failed: " + (st.error || "unknown issue")
          + (String(st.error || "").includes("Provider unavailable")
            ? " New checks stay unavailable until the provider key is configured."
            : " Nothing was committed; wait a moment and try again.");
        await refreshAfterRun();
        return;
      }
    }
    note.textContent = "The run is still going in the background; panels update when it finishes.";
  } catch (err) {
    banner("Could not start a check: " + err.message);
    note.textContent = "No new responses were recorded.";
  } finally {
    btn.disabled = Boolean(runBlockedReason());
    setRunButton("Run check");
    runInFlight = false;
    /* The note's dataset.active flag stays set: whatever the last run
       wrote (a rate limit, a verdict, a failure) should persist until the
       visitor starts the next check, not be overwritten by the baseline. */
  }
}

/* ---------- start ---------- */

hydrateIcons();
$("run-btn").addEventListener("click", runCheck);
initDemoEvents();
initTabs();
loadLive();        // API reachability as soon as liveness answers
loadHealth();      // database + provider readiness, non-blocking
ensureAwake();     // data panels, each rendering individually as they land
/* Returning to a backgrounded tab can resume long after a failed or stale
   readiness result. Refresh then; the TTL cache keeps it cheap. */
if (typeof document.addEventListener === "function") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadHealth();
  });
}
/* Periodic refresh only when connected and visible; failed endpoints are
   retried here. If nothing answers anymore, resume the wake loop. */
setInterval(() => {
  if (!runInFlight && document.visibilityState === "visible" && state.awake) {
    loadAll();
    loadLive();
    loadHealth();
    if (state.failed.size >= DATA_LOADERS.length) {
      state.awake = false;
      ensureAwake();
    }
  }
}, 60000);
