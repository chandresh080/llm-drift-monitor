# LLM Behavioral Drift Monitor — Project Report

> **Live demo:** [llm-drift-monitor-api.onrender.com](https://llm-drift-monitor-api.onrender.com) —
> dashboard at `/` (adjusted p-value history, alerts, one-click probe run). The demo runs a
> deterministic fake adapter (no API keys); free tier, sleeps after ~15 min idle.
[![CI](https://github.com/chandresh-pandey-ml/llm-drift-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/chandresh-pandey-ml/llm-drift-monitor/actions/workflows/ci.yml)

## 1. Problem and what's being monitored

We monitor **behavioral drift** in a single target LLM — the tendency of its responses
to shift over time in ways that matter for downstream use — using a fixed set of canary
prompts as probes. Each monitoring cycle sends the same versioned canary prompts to the
target and compares the **response-length / validity / embedding distributions** of a
**reference window** against the most recent **current window**.

Drift is a *signal for investigation*: without labeled ground truth, a detected shift
does not prove quality degradation — it may reflect benign variation. Quality degradation
is tracked separately via a narrow invalid-response heuristic.

## 2. System overview

```
canary prompts (versioned, 40) ──► adapter (Fake / Ollama / OpenAI-compat) ──► responses
                                                                              │
        storage (SQLite: probe_runs, response_features, embeddings, test_results, alerts)
                                                                              │
        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
        │  KS (len)    │   │  Z (valid)   │   │  MMD (emb)   │
        └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
               └───────────────┼───────────────────┼───────────┘
                               ▼      Benjamini–Hochberg (α=0.05)
                     drift? ──► alert (drift / quality) ──► MLflow + API + dashboard
```

- **Probes → responses** via target adapter; features (length, validity, embedding) stored.
- **Tests**: KS on response lengths, two-proportion z on validity, MMD on embeddings; BH correction across the 3 tests.
- **Outputs**: drift flag, alerts, MLflow run logging, API exposure (`/drift/latest`, `/runs`, `/health`), Streamlit dashboard.

## 3. Detectors and the reasoning for each

- **KS test** (response length): nonparametric two-sample comparison; appropriate for
  shift in central tendency/scale of lengths. Sensitive to length-only behavioral shifts.
- **Two-proportion z-test** (validity): binary signal of empty/invalid responses; detects
  sharp degradation in response acceptance/quality proxy.
- **MMD** (embeddings): distribution comparison via RBF kernel with median-bandwidth
  heuristic and permutation testing; captures semantic shift beyond scalar length.

Benjamini–Hochberg controls the family-wise error rate across the three predeclared tests.

## 4. Calibration results

Synthetic null distributions (n_sims = 200, α = 0.05), window n = 20:

| Test | FPR (raw) | FPR (BH) |
|---|---|---|
| KS | 0.02 | 0.015 |
| Z (validity) | 0.005 | 0.0 |
| MMD | 0.0 | 0.0 |

Conclusion: detectors are well-calibrated under the null (no miscalibration requiring
bandwidth/heuristic fixes at the time of reporting).

## 5. Power curves

Power vs. injected shift size δ (std units / refusal units) across sample sizes
(KS, z, MMD) — plotted in `results/power_curves.png`, tabulated in `results/power_curves.csv`.

## 6. Detection delay vs. false alarms

90-day simulation (shift at day 45): false alarms ≈ 0 per 100 baseline days; single-window
detection delay ≈ 18 days (first alert day 63). k-of-n and CUSUM variants did not trigger
in the simulated run.

## 7. Limitations

- **Prompt-level dependence**: 40 canaries across 5 categories; shifts confined to a single
  category may be missed or over-flagged; unit of sampling (per-prompt vs per-response) matters.
- **Small samples**: power/delay conclusions drawn from modest synthetic sample sizes; real
  windows (n≈10 probes/day) are small for three-testing regimes.
- **Embedding choice**: MiniLM vs alternatives; kernel bandwidth / permutation sensitivity.
- **Provider-side changes**: hosted-provider behavior (e.g., retries, refusals, formatting)
  cannot be observed from the client side; live keys showed auth/availability failures
  (Gemini 503, Groq 403, Grok 400) during the session.

## 8. How to reproduce

```bash
# Calibration
python -m experiments.calibration --seed 42

# Power
python -m experiments.power --seed 42

# Delay / false alarms
python -m experiments.delay --seed 42

# Localization
python -m experiments.localization --seed 42

# Sensitivity
python -m experiments.sensitivity --seed 42
```

Outputs (CSVs + PNGs) land in `results/`.

## Operational state (Phases 2–4)

- **Live deployment** (Render, free tier): the API serves the dashboard UI at `/` from
  `web/` and the JSON API at the routes below. Demo history is seeded via
  `scripts/generate_seed_db.py` (50 baseline probes → injected shift → BH-flagged drift
  alerts on record), so a cold start shows real results; `POST /runs` extends it live.
  MLflow tracking is disabled on the host (`DRIFT_MONITOR_TRACKING=0`) to fit the 512 MiB
  free tier — TS local runs keep full MLflow logging.
- **MLflow**: SQLite backend (`mlflow.db`); daily runs + experiment sweeps logged via
  `tracking.py`; server via Docker (port 5000).
- **API**: FastAPI (`api.py`) — `POST /runs`, `GET /runs`, `GET /runs/{id}`, `GET /alerts`,
  `GET /drift/latest`, `GET /drift/results`, `GET /health`; dashboard at `/`.
- **Dashboard**: hosted UI (`web/`) served by the API; local Streamlit variant reads via
  the API (`DRIFT_MONITOR_API_URL`).
- **CI**: ruff + pytest + fake-adapter smoke (`.github/workflows/ci.yml`) — green on `main`.
- **Docker**: `render.yaml` (api service) and `docker-compose.yml` (api, dashboard, mlflow).

## Testing status

- `pytest`: **24 passed** ✅ · `ruff check .`: **clean** ✅ · 8/8 sequential live runs on the
  deployed instance ✅
- Coverage: BH known example, windowing, alert persistence, adapter retry (mocked).

## Known deployment limitation

Render's free tier is 512 MiB with an ephemeral filesystem: SQLite data resets on
redeploy/restart (the seeded demo history is baked into the image, so cold starts still
show results), and MLflow tracking is disabled on the host. A paid instance restores both.
