# LLM Behavioral Drift Monitor — Full Project Context

> Context document for an AI logic review via OpenRouter. Contains the complete
> state, design decisions, phase history, and current status of the project.

## 1. What the project does

A monitoring system that watches for **behavioral drift in a single (target) LLM**
using a set of synthetic "canary" prompts, comparing a **reference window** vs a
**current window** of probe responses via statistical drift tests.

Drift signals:
- **KS test** on response length (characters).
- **Two-proportion z-test** on response validity (invalid-response rate).
- **MMD** (RBF kernel, median-bandwidth heuristic, permutation test) on response embeddings.

Multiple testing: **Benjamini–Hochberg** correction over the 3 p-values at α = 0.05.

## 2. Architecture / modules (repo root)

- `main.py` — CLI entry (`--run`, `--dashboard`, `--create-ref`, `--num-probes`).
- `monitor.py` — `LLMDriftMonitor` (core pipeline), adapters:
  - `FakeLLMAdapter` (offline deterministic),
  - `LocalLLMAdapter` (Ollama over HTTP),
  - `OpenAICompatibleAdapter` (Gemini / Groq / Grok via OpenAI-compatible endpoints, env-driven).
  Contains `_post_json_with_retry` (Phase 3): retries on HTTP 429/5xx and network/timeout
  errors, exponential backoff + jitter, `max_retries` (default 3).
- `storage.py` — SQLite (`data/drift_monitor.db`): tables `probe_runs`, `response_features`,
  `embeddings`, `test_results`, `alerts` + CRUD helpers.
- `embeddings.py` — `EmbeddingExtractor` (sentence-transformers MiniLM, lazy load).
- `drift_tests.py` — `ks_test`, `two_proportion_z_test`, `mmd_test`, `benjamini_hochberg_correction`,
  `rbf_kernel`, `compute_median_bandwidth`, `mmd_squared`.
- `tracking.py` — MLflow helpers: `drift_run` context manager, `log_drift_run`, `get_git_hash`,
  `MLFLOW_TRACKING_URI` (default `sqlite:///mlflow.db`).
- `api.py` — FastAPI service (Phase 3):
  - `POST /runs` (background task → run ID), `GET /runs`, `GET /runs/{run_id}`,
  - `GET /alerts`, `GET /drift/latest`, `GET /drift/results`, `GET /health`.
  - Pydantic models: `RunResponse`, `DriftLatestResponse`, `ProbeRunResponse`, `AlertResponse`.
- `dashboard.py` — Streamlit dashboard now backed by the API via httpx (`DRIFT_MONITOR_API_URL`, default `http://localhost:8000`).
- `experiments/` — `sim.py` (sampling/shift injection), `calibration.py`, `power.py`, `delay.py`,
  `localization.py`, `sensitivity.py` (+ `__init__.py`).
- `tests/` — `test_drift_tests.py`, `test_monitor.py` (new, Phase 4), `test_adapters.py` (new, Phase 4).
- Config: `config.yaml`; canary prompts: `canary_prompts.json` (version 2.0.0, 40 prompts, categories incl. factual/reasoning/code/refusal).

## 2. Phase history and status (user's ordered plan: Phase 0 → 1 → 2 → 3 → 4 → 5)

### Phase 0 — Cleanup (NOT executed; skipped/deferred)
Planned items not implemented: adapters moved to `adapters.py` (not done), `provider` column on
`probe_runs` (not done), adapter hardening/retry/fallback (retry implemented later in Phase 3),
`/models` endpoint usage (not done).

### Phase 1 — Experiments (DONE, locked)
Implemented `experiments/` suite; ran all scripts; outputs in `results/`.
- Calibration (n_sims=200): FPR raw/BH @ α=0.05 — KS 0.02/0.015, z 0.005/0.0, MMD 0.0/0.0.
- Localization: shifted category flagged → precision = recall = 1.0.
- Delay: false alarms 0 per 100 baseline days; single-window detection delay 18 days.
- Power: 72 rows (3 tests × 6 δ × 4 n) → `power_curves.csv`.
- Sensitivity: 12 combos → `sensitivity.csv`.

### Phase 2 — MLflow (DONE, committed as HEAD `cdef6b3`)
- Installed mlflow 3.17.2; added to `requirements.txt`.
- `tracking.py` (context manager + logging); `monitor.py` logs daily runs (params/metrics/artifacts).
- Backend: SQLite (`mlflow.db`); experiments also log headline results.

### Phase 3 — FastAPI (DONE, this session)
- `api.py` created; endpoints as above; Pydantic models; JSON structured logs; retry wiring added.
- Dashboard switched to API.
- Verified: TestClient smoke ✅, failed-run path ✅, real uvicorn server run ✅ (live cycle → completed, drift flagged).
- `requirements.txt` += fastapi, uvicorn, httpx.

### Phase 4 — Packaging & CI (DONE, this session)
- `Dockerfile`, `docker-compose.yml` (api :8000, dashboard :8501 via streamlit, mlflow :5000; volumes `./data`, `./mlruns`).
- `.github/workflows/ci.yml`: checkout → setup-python 3.12 → install requirements + ruff → `ruff check .` → `pytest` → fake-adapter smoke (`pytest tests/test_drift_tests.py::TestPipelineIntegration -q`).
- `ruff.toml` added: line-length 120, lint select `["E4","E7","E9","F"]`.
- Lint cleanups applied (unused imports/vars, bare except → `except Exception`, `== True/False` → truthy checks).
- New tests: BH known example, windowing/alert persistence (`tests/test_monitor.py`), adapter retry mocked (`tests/test_adapters.py`).
- Latest state: **ruff clean ✅, pytest 24 passed ✅, fake-adapter smoke ✅**.

### Phase 5 — README as short report (NOT done — next up)

## 3. Verification state (latest)

- Compile: all modules ✅
- Tests: 24 passed ✅ (16 pre-existing detector tests incl. MMD median-heuristic high-dim, pipeline test; +2 BH known; +2 windowing; +2 alert persistence; +3 retry mocked)
- Ruff: clean ✅
- API smoke: TestClient ✅ + real uvicorn ✅ + failed-run path ✅
- Phase 1/2 experiments: run and outputs in `results/`

## 4. Known issues / open items (for logic review)

- Phase 0 items not executed.
- Phase 5 (README-as-report) pending.
- Live provider keys: Gemini key auth OK but 503 (high demand); Groq 403 (error 1010); Grok 400 (invalid key).
- `run_monitoring_cycle` requires ≥ ref_size + cur_size runs for tests; returns `tests_run: False` with reason otherwise.
- MLflow `start_run(run_type=...)` unsupported in mlflow 3.x → fixed by dropping `run_type`.
- API `/drift/latest` reads latest test result; `/drift/results` for lists.

## 5. How to run (quick reference)

- Full monitor: `python -m main --run`
- API: `uvicorn api:app --host 0.0.0.0 --port 8000`
- Dashboard: `streamlit run dashboard.py`
- Experiments: `python -m experiments.<name> --seed ...`
- Tests: `pytest`; ruff: `ruff check .`
- Docker: `docker compose up` (api, dashboard, mlflow)
