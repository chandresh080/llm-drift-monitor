# LLM Drift Monitor

[![Live Demo](https://img.shields.io/badge/Live_Demo-onrender.com-46E3B7?logo=render&logoColor=white)](https://llm-drift-monitor-api.onrender.com)
[![CI](https://github.com/chandresh080/llm-drift-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/chandresh080/llm-drift-monitor/actions/workflows/ci.yml)

A Python service that monitors changes in LLM responses using fixed canary prompts and statistical tests. It stores probe results, compares current responses with a reference window, and raises alerts when configured drift signals persist.

## Why

An LLM can change after a model, provider, or configuration update without returning errors. When labelled feedback is unavailable, response changes can be difficult to track systematically. This project uses repeatable prompts and unsupervised statistics to identify changes for review.

A drift alert means the response distribution changed. It does not, by itself, mean that answer quality got worse.

## How it works

1. **Collect responses.** Send versioned canary prompts to the configured model through a provider adapter.
2. **Extract signals.** Record response length, whether a response is non-empty, rule-based refusal and repetition indicators, and sentence embeddings.
3. **Compare matched windows.** The deployed panel probes 40 fixed prompts per cycle; 3 reference cycles (120 rows) are compared with 1 current cycle (40 rows). Failed and empty probes are still stored and counted as unavailable.
4. **Run the three inferential tests**, each at the prompt level so repeated prompts are not treated as independent observations:
   - **Paired mean-length test** on per-prompt mean length differences.
   - **Within-prompt permutation test** on response availability (valid/non-empty rate).
   - **Allocation-preserving permutation MMD** on sentence embeddings; it randomizes which observation is current within each prompt while keeping the 3:1 allocation fixed.
   - The former pooled KS and Fisher/z results are stored as `*_pooled` **exploratory diagnostics** only. They are not inferential tests and do not drive alerts.
5. **Correct and alert.** Apply Benjamini–Hochberg correction across the three inferential p-values at α = 0.05. A distribution trigger plus two consecutive flagged cycles are required before a drift alert is raised.
6. **Store results.** Save probes, features, test results, and alerts in SQLite locally or PostgreSQL when `DATABASE_URL` is set. MLflow records experiment parameters, metrics, and reports.

The embedding model is `sentence-transformers/all-MiniLM-L6-v2`.

## Statistical validity

The inferential pipeline was audited against the deployed 3:1 matched-prompt design. The legacy whole-cluster MMD swap was invalid for unequal prompt clusters: its permuted group sizes drifted from 117:39 toward 39:117, while the observed allocation is always 117:39. The pooled KS and availability tests also treated repeated prompts as independent. The tests now use the prompt as the exchangeable unit; full derivation, methodology, and limitations are in [STATISTICAL_AUDIT.md](STATISTICAL_AUDIT.md).

### Simulation evidence (synthetic, not hosted measurements)

All figures below are **Monte Carlo results from the audit benchmark**, not observations from the hosted Gemini service. The run used fixed seeds, 40 prompts with 3 reference and 1 current cycle per trial, 1,000 null trials per temporal-correlation level, 400 trials per power cell, and 200 alert sequences. Rates include Wilson 95% confidence intervals and rejection counts.

| Signal | Result (paired pipeline) | Comparison |
| --- | --- | --- |
| Null any-detector flag, ρ = 0 | 4.5% [3.4, 6.0] (45/1000) | legacy pooled pipeline 0.9% [0.5, 1.7] (9/1000) |
| Null any-detector flag, ρ = 0.30 | 4.6% [3.5, 6.1] (46/1000) | ρ = 0.60: 5.0% [3.8, 6.5] (50/1000) |
| Length shift, 0.3 within-prompt SD | 68.0% [63.3, 72.4] (272/400) | old pooled KS on lengths 3.0% [1.7, 5.2] (12/400) |
| Semantic mean shift, 0.25 | 97.8% [95.8, 98.8] (391/400) | legacy 79.2% [75.0, 82.9] (317/400) |
| Combined-shift alert detection, ρ = 0 | 100.0% [98.1, 100.0] (200/200) | legacy 81.5% [75.5, 86.3] (163/200) |
| Null alert false alarms, ρ = 0 | 1.5% [0.5, 4.3] (3/200) | ρ = 0.30: 0.5% [0.1, 2.8] (1/200) |

Important limitations: within-prompt exchangeability across cycles is an assumption, and AR(1) temporal correlation raises the paired MMD null to 6.9% [5.5, 8.6] (69/1000) at ρ = 0.30. The availability permutation is discrete and conservative (2.4% [1.6, 3.5] under the null). Shape-only semantic shifts are detected weakly: 11.5% [8.7, 15.0] (46/400) even at 2× variance. Full tables and plots are in [results/statistical_audit/SUMMARY.md](results/statistical_audit/SUMMARY.md). Older figures in [METRICS.md](METRICS.md) and [REAL_PROVIDER_EXPERIMENT.md](REAL_PROVIDER_EXPERIMENT.md) predate this pipeline and are not calibrated performance estimates for it.

## Providers and deployment

The adapter layer supports:

- **Ollama** for local models.
- **Google Gemini** through its native API.
- **OpenAI-compatible endpoints**, including Groq (tested).

The hosted demo is configured for Gemini 3.1 Flash-Lite. Its API key is supplied through the hosting environment and is not stored in the repository. The hosted service rate-limits live checks. Provider errors are reported rather than silently replaced with synthetic model responses.

History survives restarts only when `DATABASE_URL` points to a managed PostgreSQL database. Without it, the service uses local SQLite, which is ephemeral on a container with a recycled disk but remains writable for local development. If PostgreSQL is configured but cannot be initialized, the dashboard continues to serve read-only data and new monitoring runs are rejected with HTTP 503 instead of being written to SQLite. Both `/health` and `/config` report the same non-secret storage identity, including fallback state and whether new checks are enabled. The exact Render setup and restart verification steps are in [DEPLOYMENT.md](DEPLOYMENT.md).

## Run locally

Requires Python 3.12.

```bash
git clone https://github.com/chandresh080/llm-drift-monitor.git
cd llm-drift-monitor

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Generate a deterministic local history for the dashboard.
python -m scripts.generate_seed_db

# Start the API using the local synthetic adapter.
DRIFT_MONITOR_CONFIG=config/config.cloud.yaml uvicorn api:app --reload --port 8000
```

Open http://localhost:8000. The local seed uses a deterministic adapter and an injected shift so the pipeline can be tested without provider credentials. It is demo data, not evidence from a live model.

To use a hosted provider, select its configuration file and provide the corresponding API key as an environment variable. For Gemini, use `config/config.gemini.yaml` and set `GEMINI_API_KEY`. Never commit API keys or `.env` files.

## API

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health/live` | Liveness check |
| GET | `/health` | Readiness, provider and storage status |
| GET | `/config` | Effective configuration including storage backend/durability |
| GET | `/drift/latest` | Latest detector results |
| GET | `/drift/results` | Historical detector results |
| GET | `/alerts` | Alert history |
| GET | `/runs` | Probe history |
| GET | `/stats` | Aggregate statistics |
| POST | `/runs` | Start a monitoring cycle (503 if persistent storage is unavailable) |

## Tests

```bash
ruff check .
python -m pytest -q
node web/smoke.test.js
```

These checks also run in GitHub Actions. PostgreSQL integration tests are skipped unless `TEST_DATABASE_URL` points to a dedicated disposable database.

## Limitations

- The system detects response changes; it does not measure factual correctness or task success.
- The non-empty response measure is a basic availability signal, not a quality metric. Refusal and repetition indicators are rule-based heuristics.
- Category-level localization has low power at the evaluated sample sizes.
- The two-cycle persistence rule filters isolated alerts but is not a formal guarantee of sequential false-alarm control.
- Simulation results may not transfer to real traffic. The hosted provider comparison uses different models and cannot establish temporal drift within one model.
- Hosted history is only durable after PostgreSQL has been configured and verified to survive a restart or redeployment.
