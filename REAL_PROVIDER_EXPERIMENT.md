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
| Generation settings | temperature 0.0, max_tokens 512, timeout 60 s for both providers (not fully identical: only Groq exposes reasoning_effort, next row) |
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

## Extended run: five Gemini cycles with the default persistence rule

A second execution continued the same database on 2026-11-07 (start 21:32:38, final
cycle 21:39, local time), adding five Gemini cycles on top of the completed single
comparison, using the default configuration throughout: the two-cycle persistence
rule was active and the category family was enabled. The reference window stayed the
committed run's 50 Groq runs; no Groq calls were made. Command:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers \
  --resume --current-cycles 5 --out-prefix real_provider_extended
```

Outputs: `results/real_provider_extended_cycles.csv` and
`results/real_provider_extended_results.json`.

An earlier attempt at this phase (21:28) paced its cycles 25 seconds apart, the Groq
pacing, which put twenty or more requests into Gemini's rolling one-minute window on
the free tier limit of 15 requests per minute per model; two of its five cycles failed
with a 429 that surfaced cleanly through the adapter and the circuit breaker. The
committed record covers only the corrected run. The fix spaces the Gemini phase at 70
seconds per cycle, and the corrected run recorded zero errors and zero rate-limit
events; this failure and fix are otherwise treated like the others (see failures
below).

API execution: 50 Gemini calls, 50 successful, 0 failures, 0 timeouts, 0 rate-limit
events. Per-call latency: p50 1.22 to 3.00 s across cycles; p95 3.24 to 48.51 s,
where the fourth cycle contained one call that took about 48 seconds, apparently
provider-side queueing or throttling (the call succeeded). Token usage: 2,617 tokens
across the 50 calls.

Drift-monitoring results, pooled family, identical in all five cycles (reference 50
Groq runs versus the cycle's 10 Gemini runs):

| Detector | Statistic | Raw p | BH adjusted p | Significant |
| --- | --- | --- | --- | --- |
| KS (response lengths) | 0.30 | 0.395 | 1.0 | No |
| Proportion / Fisher (validity) | n/a | 1.000 | 1.0 | No |
| MMD (embeddings, 500 perms) | 0.000 | 0.978 | 1.0 | No |

`overall_drift_detected = 0` in every cycle, and no alert fired in any cycle. The
default two-cycle persistence rule was in force throughout, so the policy was
exercised exactly as configured: each cycle's pooled family was evaluated, no cycle
flagged, and the streak never left zero.

Category-wise MMD (reference 50 Groq runs versus a Gemini-only 50 run window spanning
the trailing five cycles, ten samples per category per side, all five categories
tested in every cycle, values identical across cycles):

| Category | MMD statistic | Raw p | BH adjusted p | Significant |
| --- | --- | --- | --- | --- |
| code | 0.256 | 0.202 | 0.230 | No |
| factual | 0.000 | 0.230 | 0.230 | No |
| formatting | 0.111 | 0.230 | 0.230 | No |
| reasoning | 0.091 | 0.212 | 0.230 | No |
| refusal_probe | 0.450 | 0.004 | 0.020 | Yes |

The `refusal_probe` category flags in the adjusted family at every recorded cycle.
This is the diagnostic layer working as designed: it localizes a real distributional
difference between the two models to one category, but category results do not set
the drift verdict or fire alerts by themselves. `overall_drift_detected` is computed
from the pooled family only, which never flagged, so no alert is generated; had the
pooled family flagged, the alert message would name the affected categories.

A property of the target surfaced by repetition is worth recording explicitly, and it
applies to both providers. At temperature 0.0, gemini-3.1-flash-lite returned
textually identical answers to the same prompts in every cycle: across all 90 stored
Gemini responses (nine cycles including the single-cycle run and the discarded
attempts), each of the ten prompts has exactly one distinct response text. The Groq
reference side is deterministic as well at the pinned settings: the 50 reference runs
share only 15 distinct response texts. Therefore the identical pooled and category
values across cycles above are a faithful reflection of that determinism, not stale
data: the windows are rebuilt each cycle and the p values are recomputed, but over
identical samples they cannot change. Within a deterministic adapter, repeated cycles
add no statistical evidence, and drift detection against such a target relies on the
content actually changing, as it would in a provider model update.

Determinism turns sample size into a statistical caveat that the results now surface
quantitatively. Nominal counts count stored runs; effective counts count distinct
embedding vectors, and the monitor reports both side by side: the pooled window
fields `reference_unique_responses` and `current_unique_responses`, and per category
the fields `n_reference`, `n_current`, `unique_reference`, `unique_current` and a
`pseudoreplicated` flag inside `category_tests`. Recomputing the recorded category
tests offline from the stored embeddings with the updated code reproduces the exact
statistics above, and shows how much smaller the effective samples are:

| Family | Nominal samples | Distinct observations |
| --- | --- | --- |
| Pooled reference (Groq) | 50 | 15 |
| Pooled current (trailing five Gemini cycles) | 50 | 10 |
| Per category, reference side | 10 | 2 to 4 |
| Per category, current side | 10 | 2 |

Consequently the statistically significant refusal_probe category should be read as
a consistent localization of a cross-model difference, not as calibrated inferential
evidence: its adjusted p value of 0.020 rests on four distinct reference observations
against two distinct current observations, and the permutation test's null
distribution treats repeated identical values as independent, which inflates the
confidence. The same qualification applies to the identical pooled p values across
the five cycles: they are one comparison repeated over an identical sample, not five
independent confirmations of anything.

What the extended run adds to the single-cycle phase: the persistence rule was active
and exercised under the default configuration with no override, and the category
family, which was starved in the single-cycle phase, was fully tested in every cycle,
flagging exactly one category on evidence stable across repeats.

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
4. HTTP 429 resource exhaustion on Gemini's free tier (15 requests per minute per
   model) during the first extended attempt: two of five cycles at Groq's pacing
   failed with RESOURCE_EXHAUSTED errors surfaced verbatim through the adapter. Fix:
   the Gemini phase spaces cycles 70 seconds apart; the corrected phase recorded zero
   rate-limit events.

## Limitations

- Cross-provider confounding: models differ, so the comparison validates that the
  pipeline runs end to end against real providers and measures a cross-provider
  difference, not provider behavior in isolation and not within-model drift.
- One comparison, no repetitions of an independent event: the five extended cycles
  add persistence-rule coverage and category coverage, but because both adapters are
  deterministic at temperature 0, they are repeats of the same statistical sample,
  not independent trials. Effective sample sizes are far below the nominal ones
  (pooled reference 15 distinct answers behind 50 runs, category windows two distinct
  answers behind ten runs on each side), which is now reported next to the nominal
  counts, and the flagged refusal_probe category is a consistent localization of a
  cross-model difference rather than calibrated inference. No detection-rate or
  false-alarm claims follow; the synthetic benchmarks remain the source for those
  metrics.
- Category flags are diagnostic: a significant category (refusal_probe here) does not
  change the drift verdict or fire an alert, because the verdict comes from the
  pooled family. A deployment wanting per-category paging would need a policy change
  beyond this experiment.
- A 48 second tail latency appeared in one extended cycle, presumably provider-side
  queueing; calls succeeded and no timeout was hit at the 60 second limit, but slow
  tails exist on the free tier.
- Different token accounting: the two providers return different token fields and
  count differently, so token totals are not directly comparable.

## Reproducing

Set GROQ_API_KEY and GEMINI_API_KEY in the environment (or .env), then:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers
```

Optional flags: `--reference-cycles N` (default 5), `--current-cycles N` (a value
above 1 keeps the default two-cycle persistence rule and lets the category family
accumulate its five-cycle window), `--resume` (extend an existing
data/real_providers.db by skipping the Groq reference collection), `--out-prefix`
(default `real_provider`), and `--out-dir`. The run deletes and rebuilds
`data/real_providers.db` unless resuming, so every fresh execution starts from an
empty history. Without the needed keys the script exits before any API call; it never
falls back to the fake provider.
