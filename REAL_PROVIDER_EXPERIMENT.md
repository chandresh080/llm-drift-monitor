# Real Hosted-Provider Comparison: Groq Reference versus Gemini Current

Execution date: 2026-11-07 (start 20:57:05, final cycle 20:59:05, local time).
This is a **real hosted-provider comparison**, not a synthetic benchmark and not a
temporal drift study. All results below come from the committed output files
`results/real_provider_cycles.csv` and `results/real_provider_results.json`, produced
by `experiments/real_providers.py` using the production monitoring pipeline
unchanged: the standard adapters, storage, matched windows, detectors, and alert
policy.

**Primary caveat:** this is a cross-provider, cross-model comparison. Groq's
`openai/gpt-oss-20b` and Google's `gemini-3.1-flash-lite` are different models served
by different providers. A detected shift here could not be attributed exclusively to
provider behavior even one had fired, and nothing in this experiment demonstrates
temporal drift within a single model.

## Experimental setup

| Item | Value |
| --- | --- |
| Reference provider | Groq, `openai/gpt-oss-20b`, https://api.groq.com/openai/v1 |
| Current provider | Google Gemini, `gemini-3.1-flash-lite`, native generateContent endpoint |
| Prompt panel | canary_prompts.json version 2.1.0, 40 prompts, fixed 10 prompt panel (2 per category, identical text and order across providers) |
| Reference collection | 5 Groq cycles x 10 probes = 50 runs, filling the configured 50 run reference window |
| Current cycle | 1 Gemini cycle x 10 probes |
| Generation settings | temperature 0.0, max_tokens 512, timeout 60 s for both providers |
| Groq specific setting | reasoning_effort = low (gpt-oss-20b is a reasoning model; see failures below) |
| Embedding model | sentence-transformers/all-MiniLM-L6-v2, CPU, identical preprocessing both phases |
| Detectors | KS on lengths, proportion test on validity (Fisher dispatch), permutation MMD (500 permutations), Benjamini-Hochberg at alpha 0.05, plus effect-size diagnostics; category-wise family enabled but starved (see limitations) |
| Windows | reference 50, current 10, matched prompt ids, complete cycles only |
| Config override | alerts.min_consecutive_cycles = 1 for this run (a single current cycle cannot satisfy two-cycle persistence; detector flags are reported regardless) |
| Database | data/real_providers.db, fresh on each run (gitignored) |
| API keys | read from environment / .env only; never printed or logged |

## API execution results

| Metric | Groq (reference phase) | Gemini (current cycle) |
| --- | --- | --- |
| API requests | 50 | 10 |
| Successful | 50 | 10 |
| Failed responses | 0 | 0 |
| Timeouts | 0 | 0 |
| Rate-limit events | 0 | 0 |
| Stored runs ok / failed | 50 / 0 | 10 / 0 |

Provider per-call latency (measured per individual model call by the pipeline's
existing instrumentation):

| Provider | p50 (s) | p95 (s) |
| --- | --- | --- |
| Groq | 0.49 to 0.73 across cycles | 0.98 to 1.15 across cycles |
| Gemini | 1.31 | 2.59 |

Token usage as returned by the providers:

| Provider | Calls | Prompt tokens | Completion tokens | Total tokens |
| --- | --- | --- | --- | --- |
| Groq | 50 | 435 (87 per call) | 1,484 | 1,919 |
| Gemini | 10 | 16 | 508 | 524 |

(The Gemini call repeats the system-free instruction context, so its prompt token
count per call is lower; the two providers tokenize differently.)

## Drift-monitoring results

Pooled family, reference 50 versus current 10, both windows valid and complete:

| Detector | Statistic | Raw p | BH adjusted p | Significant |
| --- | --- | --- | --- | --- |
| KS (response lengths) | 0.30 | 0.395 | 1.0 | No |
| Proportion / Fisher (validity) | n/a (Fisher has no z statistic) | 1.000 | 1.0 | No |
| MMD (embeddings, 500 perms) | 0.000 | 0.978 | 1.0 | No |

Effect-size diagnostics: centroid distance 0.152, length shift +19.2% (mean 829.8 to
989.3 characters), refusal rate 0.0 in both windows. Pooled verdict:
`overall_drift_detected = 0`, `overall_quality_degradation = 0`, no alerts generated.

Category-wise MMD: no categories were tested. This is the honest starvation case, not
a failure: the category family needs a five-cycle current window (50 runs) and this
design contributes a single 10 run cycle, so the procedure reported an empty tested
family instead of junk p values, exactly as it is specified to behave.

Detection status: **no drift detected** in this comparison. With repeating the
Gemini side over multiple cycles, the persistence rule and the category family would
become exercisable; with a single current cycle the pooled family is the only family
with data, which the result above reflects.

## Timing, kept separate by design

| Quantity | Value | Meaning |
| --- | --- | --- |
| Provider per-call latency | Groq p95 0.98 to 1.15 s, Gemini p95 2.59 s | Provider generation time per model call |
| End-to-end cycle wall time (current cycle) | 4.13 s | Full Gemini cycle including generation, features, tests, storage |
| Monitoring overhead p95 | 0.388 s per cycle | Machinery cost without model calls (`experiments/overhead.py` benchmark, from `overhead_latency.csv`) |

## Failures encountered during development, and their fixes

These are real events from earlier runs in this session, recorded because they shaped
the final configuration:

1. HTTP 403 (Cloudflare error 1010) on every Groq request from Python: the hosting
   rejects the default Python-urllib User-Agent. Fix: all outbound HTTP in the
   repository now sends a real User-Agent (`llm-drift-monitor/1.0`); covered by a
   unit test. With that header Groq returns 200.
2. Empty content from `openai/gpt-oss-20b` at the monitoring default of 128 max
   tokens: the model is a reasoning model and spent the whole budget on hidden
   reasoning before producing text (up to about 450 reasoning tokens was measured on
   harder panel prompts). Fix: the experiment pins max_tokens 512 and
   reasoning_effort low for Groq, and the adapter now treats an empty content with a
   length finish reason as a failed call rather than storing a silent empty answer.
   At 512 tokens with low effort, all 10 panel prompts returned content; two hit the
   length cap mid-answer (finish reason length with non-empty text, recorded as
   truncated but valid responses).
3. HTTP 429 rate limiting on Groq's on-demand tier (30 requests per minute) when five
   10-call cycles ran back to back with retries: earlier fix attempts observed the
   provider's own message and opened the circuit breaker, which is the intended
   protective behavior. Fix: the experiment spaces reference cycles 25 seconds apart.
   The final run recorded zero rate-limit events.

## Limitations

- Single current cycle: the persistence rule (two consecutive flagged cycles) and the
  category-wise family are structurally untestable with one current cycle; the
  min_consecutive_cycles override exists to obtain an alert decision from the pooled
  family and applies only to this experiment.
- One trial, no repetitions: this run is a single comparison, so no detection rate
  or false-alarm claims follow from it. The synthetic benchmarks remain the source
  for those metrics.
- Cross-provider confounding: models differ, so the comparison validates that the
  pipeline runs end to end against real providers and measures a cross-provider
  difference, not provider behavior in isolation and not within-model drift.
- Different token accounting: the two providers return different token fields and
  count differently, so token totals are not directly comparable.

## Reproducing

Set GROQ_API_KEY and GEMINI_API_KEY in the environment (or .env), then:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers
```

Optional flags: `--reference-cycles N` (default 5) and `--out-dir`. The run deletes
and rebuilds `data/real_providers.db`, so every execution starts from an empty
history. Without both keys the script exits before any API call; it never falls back
to the fake provider.
