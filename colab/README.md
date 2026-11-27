# LLM Drift Monitor: Colab (free CPU runtime)

`drift_monitor_colab.ipynb` is a reproducible walkthrough of the **current**
project code. It clones this repository, installs dependencies from
`requirements.txt`, and calls the real `LLMDriftMonitor` pipeline. It does not
reimplement the detectors, upload modules by hand, or paste an API key into a
notebook cell.

## Run it

1. Open `drift_monitor_colab.ipynb` in Colab. A CPU runtime is sufficient.
2. Add the provider key to **Colab Secrets** (recommended) or set it as an
   environment variable before the notebook starts:
   - `GEMINI_API_KEY` for the primary live demo (`gemini-3.1-flash-lite`
     through the native Gemini adapter).
   - Optional: `GROQ_API_KEY` / `GROQ_MODEL`, or `XAI_API_KEY` (or
     `OPENAI_API_KEY`) for the OpenAI-compatible providers.
3. Run all cells top to bottom.

Secrets are read with `google.colab.userdata.get()` in Colab, falling back to
ordinary environment variables outside Colab. Keys are never printed or
written to a config file. If no credential is available, the notebook
explicitly switches to the repository's `FakeLLMAdapter` and labels all output
as a **controlled simulation**, not a live provider measurement.

## What the notebook does, in order

1. Clones `https://github.com/chandresh080/llm-drift-monitor.git` and installs
   exactly `requirements.txt`.
2. Loads `colab/config.yaml`, forces a runtime-local SQLite path, and selects a
   provider. `DATABASE_URL` is cleared so the notebook cannot accidentally
   write to the hosted database.
3. Verifies the adapter and storage, then collects **3 reference cycles** of
   10 probes each. These cycles intentionally report `tests_run: false`;
   a detector needs a complete reference window and a disjoint current window.
4. Runs **2 evaluation cycles**. With `reference.window_size: 30` and
   `monitoring.batch_size: 10`, the 4th cycle is the first one that can run the
   full detector family.
5. Reports detector statistics and BH-adjusted p-values, effect sizes,
   quality/distribution separation, alert status and persistence, a compact
   cycle history, and plots of adjusted p-values and effect sizes.

The fresh-runtime path therefore needs 5 cycles / 50 provider calls, not one
cycle. If a provider call fails, the cycle is recorded as failed, the error is
shown without the key, and later cells handle the absence of results.

## Three different things this folder can show

- **Local Colab experimentation:** this notebook; small windows, local SQLite,
  real project modules, optional live provider, runtime-local results.
- **Hosted live Gemini demo:** the deployment uses
  `config/config.gemini.yaml` (native Gemini, 40 probes, 120-run reference,
  two-cycle alert persistence) and PostgreSQL through `DATABASE_URL`. Its
  `/health` endpoint is the authority for the deployed backend. The notebook
  deliberately does not inherit that configuration or database.
- **Controlled simulation / benchmark:** the `fake` adapter or an experiments
  script. Detection delays, false-positive rates, and alarm counts from these
  runs are not live production measurements.

## Storage and durability

`storage.py` in the repository supports both backends: SQLite by default and
PostgreSQL when `DATABASE_URL` is set. In Colab the notebook uses SQLite at
`/content/drift_monitor.db` (overridable with `DRIFT_MONITOR_DB_PATH`). That
file is **runtime-local**: it can disappear when the Colab runtime is reset.
It is not the hosted PostgreSQL store and must not be described as durable.

## Useful environment overrides

| Variable | Meaning |
| --- | --- |
| `DRIFT_MONITOR_PROVIDER` | `auto` (default), `gemini`, `groq`, `xai`, or `fake` |
| `DRIFT_MONITOR_REFERENCE_CYCLES` | Reference cycles before evaluation (default `3`) |
| `DRIFT_MONITOR_EVAL_CYCLES` | Evaluation cycles to run (default `2`) |
| `DRIFT_MONITOR_RESET_DB` | `1` (default) starts from a clean local DB |
| `DRIFT_MONITOR_DB_PATH` | Override the runtime-local SQLite path |
| `DRIFT_MONITOR_CONFIG_PATH` | Override the generated runtime YAML path |
| `DRIFT_MONITOR_REPO_URL` / `DRIFT_MONITOR_REPO_DIR` | Override clone source/destination |

Free-tier quotas apply. If you see repeated 429s, increase
`target.call_spacing_seconds` in `colab/config.yaml` or reduce the number of
cycles. A detected shift is a prompt for investigation; it is not by itself
proof of quality degradation.
