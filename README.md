# LLM Drift Monitor

A monitoring tool that watches whether an LLM's answers quietly change over time, and
tells you when they do.

Live demo: [llm-drift-monitor-api.onrender.com](https://llm-drift-monitor-api.onrender.com)
(dashboard at `/`). It runs a scripted fake model, so nothing you do there calls a real
provider or costs anything.

[![CI](https://github.com/chandresh-pandey-ml/llm-drift-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/chandresh-pandey-ml/llm-drift-monitor/actions/workflows/ci.yml)

## The problem

Suppose your product answers come from an LLM. Nothing about that LLM is pinned down.
Providers update models, add safety layers, change default settings, or quietly route
you somewhere else, and each of those can change what your users see. The failure is
invisible: no exception fires, the status code stays 200, answers just get longer,
start refusing more often, or drift in topic. You find out from a complaint, possibly
weeks later.

Checking by hand doesn't work either. One answer tells you almost nothing, because
models vary naturally from answer to answer. And reading dozens of answers every
morning doesn't scale.

## How this project handles it

The monitor runs the same 40 test prompts against the target model on a schedule and
stores every answer in SQLite. Each check then compares a batch of recent answers with
a saved baseline, using three small statistical tests:

1. **Answer length**: a KS test asks whether recent answers are noticeably longer or
   shorter than the baseline ones.
2. **Useful answers**: a two-proportion test on how often answers come back empty.
   A jump here usually means the model started refusing or breaking.
3. **What answers mean**: answers are turned into text embeddings, and an MMD test
   (kernel based, permutation p value) asks whether the overall meaning mix changed,
   which catches topic or tone drift that length and counts miss.

Three tests at once should not make three chances to cry wolf, so raw p values go
through Benjamini-Hochberg correction at alpha = 0.05 before anything is flagged.
When tests fire, the monitor writes an alert. Nothing is auto-panic: a flag means
"look at the answers", and a person decides.

## Does it actually work

Evaluated on synthetic data where the truth is known, using `experiments/`:

- False alarm rate under the null (200 simulations): KS 0.02, z 0.005 of raw p values,
  MMD and BH-corrected versions 0.015 or lower. In words: when nothing changed, the
  tests almost never cried wolf.
- A mismatch of 20 per cent on a MMD bandwidth was caught, then fixed with the median
  heuristic, so calibration held after the fix.
- On a 90 day simulation with a change injected at day 45: no alerts during the 100
  quiet days, and the drift was caught about 18 days after the change started.
- Power measurements across shift sizes and window sizes say how many probes a day
  you need for a given change, so the prompt count isn't a guess.
- Only a one category shifted test pointed at the right category (precision and recall
  of 1.0 in that experiment), so localization is feasible, not perfect.

Conclusions and CSV/PNG outputs for all of this are in `results/`, and every script
takes a `--seed`, so the numbers above can be regenerated.

## Try it

- Live demo: open the URL above and press "Run a check now". Ten scripted answers get
  stored, panels refresh. At least 60 saved answers are needed for the tests to run,
  and the demo history already contains a batch where the answers deliberately changed,
  so the alert panel has real entries on a fresh visit.
- Local, no keys needed: `python -m scripts.generate_seed_db`, then
  `uvicorn api:app --port 8000` and open `http://localhost:8000`.
- With your own local model (Ollama): point `config.yaml` at `target.type: local`
  and run `python -m main --run` as a daily cron job.

The service is also a plain JSON API: `GET /health`, `GET /drift/latest`,
`GET /drift/results`, `GET /alerts`, `GET /runs`, `GET /stats`, `POST /runs`. The
dashboard is a thin page over it.

## Repo layout

- `monitor.py`: the pipeline: adapters (fake, Ollama, OpenAI-compatible), probe runs,
  window comparisons, alerts. Retries with backoff on 429, 5xx, timeouts.
- `drift_tests.py`: the three tests plus BH correction and the RBF kernel.
- `storage.py`: SQLite schema and queries.
- `embeddings.py`: MiniLM embeddings for the MMD test.
- `api.py`: FastAPI service; `web/` is the dashboard page it serves.
- `scripts/generate_seed_db.py`: builds the demo history with a baked-in shift.
- `experiments/`: the evaluation described above.
- `tracking.py`: MLflow logging for local runs.

## Testing

`pytest` (24 tests) and `ruff check .` run in CI on every push. The tests cover the BH
correction against a hand-checked example, the reference window logic, alert storage,
and the retry behaviour of the network adapters with a mocked transport.

## Honest limitations

- A flag is a hint, not proof of bad quality: no labeled ground truth exists here, so
  a change can also be benign (an update made answers friendlier).
- 40 prompts across 5 categories is a small lens: a shift in an untested behavior
  would not be seen.
- The numbers in "Does it actually work" come from simulations; real traffic windows
  are noisier than the synthetic ones.
- On the free hosting tier the database resets on redeploys and MLflow logging is
  switched off; locally both work fully.
