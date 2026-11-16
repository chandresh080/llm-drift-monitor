# LLM Drift Monitor

A Python service that monitors changes in LLM responses using fixed canary prompts and statistical tests. It stores probe results, compares current responses with a reference window, and raises alerts when configured drift signals persist.

[Live demo](https://llm-drift-monitor-api.onrender.com) · [CI](https://github.com/chandresh080/llm-drift-monitor/actions/workflows/ci.yml)

## Why

An LLM can change after a model, provider, or configuration update without returning errors. When labelled feedback is unavailable, response changes can be difficult to track systematically. This project uses repeatable prompts and unsupervised statistics to identify changes for review.

A drift alert means the response distribution changed. It does not, by itself, mean that answer quality got worse.

## How it works

1. **Collect responses.** Send versioned canary prompts to the configured model through a provider adapter.
2. **Extract signals.** Record response length, whether a response is non-empty, rule-based refusal and repetition indicators, and sentence embeddings.
3. **Compare windows.** Apply three statistical tests:
   - **KS test** on response lengths.
   - **Two-sample proportion test** on non-empty response rates, with Fisher's exact test for small counts.
   - **Permutation-based MMD** on sentence embeddings to detect semantic distribution changes.
4. **Correct and alert.** Apply Benjamini–Hochberg correction across the detector family. By default, a flagged result must persist for two consecutive cycles before an alert is raised.
5. **Store results.** Save probes, features, test results, and alerts in SQLite. MLflow records experiment parameters, metrics, and reports.

The embedding model is `sentence-transformers/all-MiniLM-L6-v2`.

## Providers and deployment

The adapter layer supports:

- **Ollama** for local models.
- **Google Gemini** through its native API.
- **OpenAI-compatible endpoints**, including Groq (tested).

The hosted demo is configured for Gemini 3.1 Flash-Lite. Its API key is supplied through the hosting environment and is not stored in the repository. The hosted service rate-limits live checks. Provider errors are reported rather than silently replaced with synthetic model responses.

## Evaluation

The reported metrics below come from controlled simulations with known shifts. They measure the tested configuration, not guaranteed performance on production traffic.

| Measure | Result | Evaluation setup |
| --- | --- | --- |
| Any-detector false-positive rate after BH correction | 1.9% | 1,000 null simulations |
| Detection rate | 100% | 100 simulated 90-day trials; injected 1.2σ shift |
| Mean detection delay | 0.97 days | Same 1.2σ shift scenario |
| False alarms | 0.18 per 100 quiet days | Same 90-day simulation; 45 quiet days per trial |

<br>

The delay simulation injects a 1.2σ shift in response lengths and embeddings and reduces the valid-response rate from 0.95 to 0.70 after day 45. These results should not be read as detection guarantees for smaller or different changes. Category-level localization is weak at the evaluated sample size: recall was 20% at a 3σ shift. Full methodology and results are in [METRICS.md](METRICS.md).

The [Groq-versus-Gemini experiment](REAL_PROVIDER_EXPERIMENT.md) is a comparison between different models and providers. It is not a temporal drift study of one model.

## Run locally

Requires Python 3.12.

```bash
git clone https://github.com/chandresh-pandey-ml/llm-drift-monitor.git
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
| GET | `/health` | Readiness and provider status |
| GET | `/drift/latest` | Latest detector results |
| GET | `/drift/results` | Historical detector results |
| GET | `/alerts` | Alert history |
| GET | `/runs` | Probe history |
| GET | `/stats` | Aggregate statistics |
| POST | `/runs` | Start a monitoring cycle |

## Tests

```bash
pytest
ruff check .
node web/smoke.test.js
```

These checks also run in GitHub Actions.

## Limitations

- The system detects response changes; it does not measure factual correctness or task success.
- The non-empty response measure is a basic availability signal, not a quality metric. Refusal and repetition indicators are rule-based heuristics.
- Category-level localization has low power at the evaluated sample sizes.
- The two-cycle persistence rule filters isolated alerts but is not a formal guarantee of sequential false-alarm control.
- Simulation results may not transfer to real traffic. The hosted provider comparison uses different models and cannot establish temporal drift within one model.
