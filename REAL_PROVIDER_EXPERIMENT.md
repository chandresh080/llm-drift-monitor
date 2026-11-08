# Real Hosted-Provider Comparison: Groq Reference versus Gemini Current

Execution date: 2026-11-08 (single cycle comparison start 10:00:41, extended run start
10:02:49, local time). The experiment was first executed and recorded on 2026-11-07;
both committed result artifacts were regenerated on 2026-11-08 after an effective
sample size gate was added to the category family, so every number below describes
the committed artifacts exactly. This is a **real hosted-provider comparison**, not a
synthetic benchmark and not a temporal drift study. All results come from the output
files `results/real_provider_cycles.csv`, `results/real_provider_results.json`,
`results/real_provider_extended_cycles.csv` and `results/real_provider_extended_results.json`,
produced by `experiments/real_providers.py` using the production monitoring pipeline
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
| Current stage | 1 Gemini cycle in the comparison run, then 5 more Gemini cycles in the extended run |
| Generation settings | temperature 0.0, max_tokens 512, timeout 60 s for both providers (not fully identical: only Groq exposes reasoning_effort, next row) |
| Groq specific setting | reasoning_effort = low (gpt-oss-20b is a reasoning model; see failures below) |
| Embedding model | sentence-transformers/all-MiniLM-L6-v2, CPU, identical preprocessing both stages |
| Detectors | KS on lengths, proportion test on validity (Fisher dispatch), permutation MMD (500 permutations), Benjamini-Hochberg at alpha 0.05, plus effect-size diagnostics; category-wise family gated by EFFECTIVE sample counts (see the gating section) |
| Windows | reference 50, current 10, matched prompt ids, complete cycles only; the category current window spans the trailing five complete cycles |
| Config override | alerts.min_consecutive_cycles = 1 for the single cycle run (a single current cycle cannot satisfy two-cycle persistence); the extended run uses the default value of 2 |
| Database | data/real_providers.db, fresh on each full run (gitignored); `--resume` keeps the reference window |
| API keys | read from environment / .env only; never printed or logged |

## Single cycle comparison: API execution results

| Metric | Groq (reference stage) | Gemini (current cycle) |
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
| Groq | 0.47 to 0.63 across cycles | 0.57 to 1.04 across cycles |
| Gemini | 1.31 | 3.91 |

Token usage as returned by the providers:

| Provider | Calls | Prompt tokens | Completion tokens | Total tokens |
| --- | --- | --- | --- | --- |
| Groq | 50 | 441 | 1,884 | 2,325 |
| Gemini | 10 | 16 | 508 | 524 |

(The two providers tokenize differently and return different usage fields, so token
totals are not directly comparable.)

## Single cycle comparison: drift-monitoring results

Pooled family, reference 50 versus current 10, both windows valid and complete:

| Detector | Statistic | Raw p | BH adjusted p | Significant |
| --- | --- | --- | --- | --- |
| KS (response lengths) | 0.30 | 0.395 | 1.0 | No |
| Proportion / Fisher (validity) | n/a (Fisher has no z statistic) | 1.000 | 1.0 | No |
| MMD (embeddings, 500 perms) | 0.000 | 0.978 | 1.0 | No |

Effect-size diagnostics: centroid distance 0.153, length shift +19.4% (mean 828.4 to
989.3 characters), refusal rate 0.0 in both windows. Pooled verdict:
`overall_drift_detected = 0`, `overall_quality_degradation = 0`, no alerts generated.

Effective sample sizes are now reported next to the nominal window size in the
committed JSON: `reference_unique_responses` is 12 of a nominal 50, and
`current_unique_responses` is 10 of a nominal 10. The reference window was collected
at temperature 0 from the same ten prompts five times, so fifty stored runs carry
only twelve distinct answers; any pooled p value over that window inherits the
pseudoreplication caveat spelled out below. Single Gemini cycle pooled results: no
drift detected, no alerts.

Category-wise MMD: no categories were tested, reported as an empty tested family.
Two reasons stack: the category current window needs five complete Gemini cycles
(50 runs) and this run contributes one, and the gate below now requires enough
DISTINCT observations even where the nominal count exists. The procedure reports the
starvation rather than junk p values, exactly as it is specified to behave.

## Effective sample size gating: what changed and why

The first recording of this experiment (2026-11-07) ran with the category family
gated on nominal sample counts. Five repeated Gemini cycles with identical answers
reported `refusal_probe` as BH significant (MMD statistic 0.450, raw p 0.004,
adjusted p 0.020). Recount shown at that time: the ten nominal runs per category
were two to four distinct reference observations versus two distinct current
observations. A permutation null that treats repeated identical values as
independent inflates confidence, so that adjusted p value did not support the
significance it displayed. The inference has been **withdrawn**.

The fix keeps the category family's existing design ("a thin category cannot produce
a junk p value") and applies it to the information that is actually present: the
minimum `min_samples_per_category` gate now counts distinct embedding vectors per
side. In the current committed extended artifact every category is therefore
skipped, with counts reported in place of p values:

| Category | Reference: distinct of nominal | Current: distinct of nominal | Decision |
| --- | --- | --- | --- |
| code | 3 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| factual | 3 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| formatting | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| reasoning | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| refusal_probe | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |

The largest category distance observed before the gate existed (refusal_probe,
0.450 on 2026-11-07 data) remains in the narrative as a descriptive observation, not
an inferential one. Calendar concentration: the withdrawal applies to the recorded
2026-11-07 flag, and no category p value is emitted by the current pipeline for the
stored windows.

The pooled family is unchanged by the gate: all three tests keep treating each
stored run as an observation, because the pooled windows are the shared alerting
path for every adapter including stochastic ones. Its pseudoreplication is exposed
instead of hidden (`reference_unique_responses` / `current_unique_responses` in the
JSON), and the qualification follows: with 12 distinct answers behind 50 reference
runs, the pooled p values over deterministic windows are a description of the
comparison between two fixed answer sets, not a calibrated test of generative
distributions. The honest fix for the pooled side, as for categories, is more
distinct prompts per sample or a prompt level dependence aware test, not the
reported flag alone.

## Extended run: five Gemini cycles with the default persistence rule

A second execution continued the same database on 2026-11-08 (start 10:02:49),
attempting five Gemini cycles on top of the completed single comparison, using the
default configuration throughout: the two-cycle persistence rule active, and the
category gate on distinct observations. The reference window stayed the rerun's
50 Groq runs; no Groq calls were made. Command:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers \
  --resume --current-cycles 5 --out-prefix real_provider_extended
```

Execution was not clean end to end and is reported as it happened: cycle 1 failed
with Gemini HTTP 429 RESOURCE_EXHAUSTED (the free tier's per model request counter
was still carrying the single cycle run's ten Gemini calls from two minutes
earlier), all ten calls of that cycle failed, and zero runs were stored for it. The
remaining four cycles succeeded; this is recorded in the committed artifact rather
than being cleaned up afterwards.

API execution in the committed artifact:

| Cycle | Stored runs | Outcome |
| --- | --- | --- |
| 1 | 0 | HTTP 429 RESOURCE_EXHAUSTED, all ten calls failed, no runs stored |
| 2 to 5 | 10 each | eight of the value's tests run in each; 40 calls, all successful |

Per-call latency across the four stored cycles: p50 1.26 to 1.60 s, p95 3.05 to
15.46 s (the third stored cycle contained one call that took about 15 seconds,
presumably provider-side queueing). Token usage: 1,293 tokens across the 40
successful calls.

Pooled drift results are identical in all four stored cycles (KS statistic 0.30 raw
p 0.395, Fisher p 1.000, MMD statistic 0.000 raw p 0.978, all adjusted p 1.0):
`overall_drift_detected = 0` in every cycle and no alert fired. This identity is the
pseudoreplication made visible: each cycle rebuilds its windows and recomputes its
permutations, but the reference window and the per cycle current answers are the
same fixed answer sets every time, so the numbers cannot change. Repeating cycles
against a deterministic target adds no statistical evidence.

## What the artifacts say about determinism

Both providers behaved deterministically at the pinned settings in these runs, on
top of the earlier direct measurement (2026-11-07, on the database that existed
before the rerun: one distinct response text per panel prompt across 90 stored Gemini
responses, and 50 Groq reference runs carrying 15 distinct texts). In the current
artifacts, `current_unique_responses` is 10 in every stored cycle against an
unchanging reference of 12 distinct answers in 50 runs, and the pooled p values are
identical to the sixteen digit precision in every tested cycle. Groq's fresh
reference window carried 12 distinct texts this time rather than 15, which shows the
reference side is not perfectly frozen across reruns, in a small way that the
reported unique counts make visible.

## Timing, kept separate by design

| Quantity | Value | Meaning |
| --- | --- | --- |
| Provider per-call latency | Groq p95 0.57 to 1.04 s, Gemini p95 3.05 to 15.46 s across stored cycles | Provider generation time per model call |
| End-to-end cycle wall time (last single cycle Gemini cycle) | 4.65 s | Full Gemini cycle including generation, features, tests, storage |
| Monitoring overhead p95 | 0.388 s per cycle | Machinery cost without model calls (`experiments/overhead.py` benchmark, from `overhead_latency.csv`) |

## Failures encountered during development, and their fixes

These are real events from the runs in this session, recorded because they shaped
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
3. HTTP 429 rate limiting on Groq's on-demand tier (30 requests per minute) when
   five 10-call cycles ran back to back with retries: the provider's own message
   surfaced through the adapter and the circuit breaker opened, which is the intended
   protective behavior. Fix: the experiment spaces reference cycles 25 seconds
   apart. The final run recorded zero rate-limit events.
4. HTTP 429 resource exhaustion on Gemini's free tier (15 requests per minute per
   model) during the first extended attempt on 2026-11-07: two of five cycles at
   Groq's 25 second pacing failed with RESOURCE_EXHAUSTED errors surfaced verbatim
   through the adapter. Fix: the Gemini stage spaces cycles 70 seconds apart; the
   corrected recording completed five of five cycles with zero errors.
5. HTTP 429 again on extended cycle 1 on 2026-11-08 after the artifact
   regeneration: the rerun of the single cycle comparison completed two minutes
   earlier and its ten Gemini calls still counted against the per model window, so
   the extended run's first burst was rejected. Left in the record as-is: one failed
   cycle with zero stored runs, four clean cycles after it. The general lesson stands
   for consecutive experiment invocations as well as for cycles inside one
   invocation: Gemini's free tier needs at least a minute of spacing after any
   burst.

## Limitations

- Cross-provider confounding: models differ, so the comparison validates that the
  pipeline runs end to end against real providers and measures a cross-provider
  difference, not provider behavior in isolation and not within-model drift.
- Determinism bounds the statistics: both adapters ran at temperature 0, so the
  pooled windows describe two fixed answer sets rather than sampling from
  generative distributions. The pooled family keeps counting every stored run as an
  observation (it is the shared alerting path for all adapters), its unique counts
  are reported so the gap between nominal and effective samples is visible
  (12 distinct of 50 reference runs in these artifacts), and every identical p value
  across cycles in these artifacts is one comparison, not independent confirmation.
  The category family no longer emits p values at all for these windows.
- The flagged category was withdrawn: the 2026-11-07 recording marked refusal_probe
  BH significant at 0.020; recounted effective samples (four distinct reference
  observations versus two distinct current observations) showed the confidence was
  inflated by pseudoreplication, so the inference is retracted here and the gate now
  prevents it structurally. The descriptive distance (MMD statistic 0.450, the
  largest of the five) stays in the narrative only.
- No detection-rate or false-alarm claims follow from any of this; the synthetic
  benchmarks remain the source for those metrics, and a within-model temporal
  comparison (one model, two versions) remains the missing demonstration for
  production drift claims.
- Slow tail latencies exist on the free tier: one 48 second call (2026-11-07) and
  one 15 second call (2026-11-08) succeeded without hitting the 60 second limit.
- Different token accounting: the two providers return different token fields and
  count differently, so token totals are not directly comparable.
- Extended cycle 1's recorded 429 leaves the extended run at four stored cycles, not
  five: the persistence rule ran unmodified over the cycles that stored results.

## Reproducing

Set GROQ_API_KEY and GEMINI_API_KEY in the environment (or .env), then:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers
```

Optional flags: `--reference-cycles N` (default 5), `--current-cycles N` (a value
above 1 keeps the default two-cycle persistence rule and lets the category window
accumulate five cycles; the effective sample gate still applies inside it),
`--resume` (extend an existing data/real_providers.db by skipping the Groq
reference collection; wait for the provider's rate window to clear first),
`--out-prefix` (default `real_provider`), and `--out-dir`. The run deletes and
rebuilds `data/real_providers.db` unless resuming, so every fresh execution starts
from an empty history. Without the needed keys the script exits before any API
call; it never falls back to the fake provider.
