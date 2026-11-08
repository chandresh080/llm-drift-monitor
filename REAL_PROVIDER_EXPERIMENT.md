# Real Hosted-Provider Comparison: Groq Reference versus Gemini Current

Execution date: 2026-11-08 (single cycle comparison start 11:53:35, extended run start
11:57:16, local time). The experiment was first executed and recorded on 2026-11-07;
both committed result artifacts were regenerated three times on 2026-11-08, most
recently after the pooled MMD was joined by a paired prompt level permutation that
treats the matched prompts, not the stored runs, as the exchangeable units, so every
number below describes the committed artifacts exactly. This is a **real
hosted-provider comparison**, not a
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
| Detectors | KS on lengths, proportion test on validity (Fisher dispatch), permutation MMD (500 permutations, duplicate embeddings collapsed at entry), paired prompt level MMD diagnostic (exhaustive 1024 swap patterns on the 10 matched prompts), Benjamini-Hochberg at alpha 0.05, plus effect-size diagnostics; category-wise family gated by DISTINCT observation counts (see the pseudoreplication section) |
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
| Groq | 0.454 to 0.686 across cycles | 0.899 to 1.231 across cycles |
| Gemini | 1.722 | 6.531 |

Token usage as returned by the providers:

| Provider | Calls | Prompt tokens | Completion tokens | Total tokens |
| --- | --- | --- | --- | --- |
| Groq | 50 | 449 | 1,463 | 1,912 |
| Gemini | 10 | 13 | 78 | 91 |

(The two providers tokenize differently and return different usage fields, so token
totals are not directly comparable.)

## Single cycle comparison: drift-monitoring results

Pooled family, reference 50 versus current 10, both windows valid and complete:

| Detector | Statistic | Raw p | BH adjusted p | Significant |
| --- | --- | --- | --- | --- |
| KS (response lengths) | 0.30 | 0.395 | 1.0 | No |
| Proportion / Fisher (validity) | n/a (Fisher has no z statistic) | 1.000 | 1.0 | No |
| MMD (embeddings, 500 perms, deduplicated) | 0.000 | 0.980 | 1.0 | No |
| Paired prompt level MMD (diagnostic, 1024 swap patterns) | -0.027 | 1.0 | n/a, reported raw | No |

Effect-size diagnostics: centroid distance 0.152, length shift +20.2% (mean 823.1 to
989.3 characters), refusal rate 0.0 in both windows. Pooled verdict:
`overall_drift_detected = 0`, `overall_quality_degradation = 0`, no alerts generated.

Effective sample sizes are reported next to the nominal window size in the committed
JSON: `reference_unique_responses` is 12 of a nominal 50, and
`current_unique_responses` is 10 of a nominal 10. The reference window was collected
at temperature 0 from the same ten prompts five times, so fifty stored runs carry
only twelve distinct answers; those twelve, not fifty, are what MMD computes over.
Single Gemini cycle pooled results: no drift detected, no alerts.

Category-wise MMD: no categories were tested, reported as an empty tested family.
Two reasons stack: the category current window needs five complete Gemini cycles
(50 runs) and this run contributes one, and the gate below now requires enough
DISTINCT observations even where the nominal count exists. The procedure reports the
starvation rather than junk p values, exactly as it is specified to behave.

## Pseudoreplication: what is deduplicated and what is not

The first recording of this experiment (2026-11-07) ran with the category family
gated on nominal sample counts. Five repeated Gemini cycles with identical answers
reported `refusal_probe` as BH significant (MMD statistic 0.450, raw p 0.004,
adjusted p 0.020). Recount shown at that time: the ten nominal runs per category
were two to four distinct reference observations versus two distinct current
observations. A permutation null that treats repeated identical values as
independent inflates confidence, so that adjusted p value did not support the
significance it displayed. The inference has been **withdrawn**.

Two corrections followed, in the order they were applied:

1. The category family's minimum `min_samples_per_category` gate now counts distinct
   embedding vectors per side, keeping that family's existing design ("a thin
   category cannot produce a junk p value"). In the current committed extended
   artifact every category is therefore skipped, with counts reported in place of
   p values:

| Category | Reference: distinct of nominal | Current: distinct of nominal | Decision |
| --- | --- | --- | --- |
| code | 3 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| factual | 3 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| formatting | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| reasoning | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |
| refusal_probe | 2 of 10 | 2 of 10 | skipped, insufficient distinct responses |

2. `mmd_test` itself now collapses duplicate embeddings at its entry, in first seen
   order, so BOTH the category path and the pooled path compute over the distinct
   observations rather than repeats of them. A fifty run reference window carrying
   twelve distinct answers is a twelve point window to the test, and ten copies of
   one answer count as one observation. Two windows of all identical vectors return
   the conservative (statistic 0.0, p 1.0). The on disk pooled MMD changed
   accordingly across the two regenerations: raw p 0.978 before the collapse (the
   duplicates have mass in the median bandwidth heuristic), 0.980 after, with the
   conclusion (no drift, no alerts) unchanged.

What is deliberately NOT deduplicated: the KS test on response lengths and the
validity proportion test keep every stored run. Different texts with equal lengths
are different observations, and a failed call is a real event even when a previous
cycle failed identically; collapsing those would understate the very frequencies
those tests measure. This asymmetry is a stated design decision, not an oversight:
MMD operates in embedding space where identical answers are literally the same
point, while the count-based tests operate on labels where identical labels carry
independent per run information.

Exact deduplication also has a stated trade: two different prompts can legitimately
produce identical embeddings, and collapsing them counts those as one observation.
That is the conservative direction, and it costs nothing in these windows because
the twelve distinct reference answers sit on ten distinct prompts; in generality it
is a defensible compression of repeated evidence, not a universal definition of
effective sample size.

## Paired prompt level permutation: the dependence actually in the design

The point level deduplication above removes exact repeats, but the panel repeats
prompts, not just answers: every cycle runs the same ten matched prompt ids on both
sides, so five different answers to one prompt are five observations that share a
prompt as a source of variation. A run level permutation ignores that structure.
The pooled diagnostics now include a second MMD permutation
(`paired_cluster_mmd_test`) whose exchangeable unit is the matched prompt: each of
the 1024 swap patterns exchanges the complete reference and current answer sets of
whole prompts, everything enumerated exhaustively, with the unpermuted assignment
inside the null so the smallest attainable p value is 1/1024.

In the committed artifacts this diagnostic reports statistic -0.027 (MMD^2 can sit
slightly negative when the two sides coincide exactly, which the observed
assignment does: same answers, same prompts), raw p 1.0 with all ten prompt pairs
matched, identical in every stored cycle. It is reported as a diagnostic alongside
the standard pooled MMD, not wired into alerts: the alerting path stays the one
definition shared by every adapter, and the paired test assumes a matched prompt
panel that arbitrary (for example, free form text) adapters do not provide. Where
the panel is matched, as here, the paired number is the one that respects the
design's dependence structure. It does not cure the sample size: ten prompts per
side carry ten exchangeable units, which is why the honest quantification of these
windows remains a description of two fixed answer sets, and why more distinct
prompts remains the named future work.

The largest category distance observed before the gate existed (refusal_probe,
0.450 on 2026-11-07 data) remains in the narrative as a descriptive observation, not
an inferential one. The withdrawal applies to the recorded 2026-11-07 flag, and no
category p value is emitted by the current pipeline for the stored windows.

The pooled family's remaining qualification, unchanged by the paired test: twelve
distinct answers against ten describe a comparison between two fixed answer sets,
not a test of generative distributions, and ten matched prompts are ten
exchangeable units. The point level deduplication and the paired permutation
correct the arithmetic; the honest fix for the information content itself is more
distinct prompts per panel.

## Extended run: five Gemini cycles with the default persistence rule

A second execution continued the same database on 2026-11-08 (start 11:57:16,
90 seconds after the single cycle comparison finished so Gemini's per model quota
window had fully cleared), attempting five Gemini cycles on top of the completed
single comparison, using the default configuration throughout: the two-cycle
persistence rule active, the distinct observation gate in the category family, and
the paired prompt level diagnostic in the pooled results. The reference window
stayed the comparison run's 50 Groq runs; no Groq calls were made. Command:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers \
  --resume --current-cycles 5 --out-prefix real_provider_extended
```

Execution was clean end to end: five of five cycles stored results, 50 Gemini calls
all successful, zero errors and zero rate-limit events. This required the spacing
above; an earlier regeneration attempt on the same day (failure 5 below) had its
first cycle rejected with HTTP 429 because the previous invocation's ten Gemini
calls were still inside the quota window, and that event is kept in the failure
history as the documented lesson.

API execution in the committed artifact:

| Cycle | Stored runs | Outcome |
| --- | --- | --- |
| 1 to 5 | 10 each | all 50 calls successful, tests run in every cycle |

Per-call latency across the five cycles: p50 1.435 to 2.479 s, p95 2.974 to
15.381 s (the fourth cycle contained one call that took about 15 seconds,
presumably provider-side queueing). Token usage: 2,184 tokens total (81 prompt,
2,103 completion) across the 50 successful calls.

Pooled drift results are identical in all five cycles (KS statistic 0.30 raw p
0.395, MMD statistic 0.000 raw p 0.980, paired prompt level MMD statistic -0.027
raw p 1.0, computed over the same windows of 12 distinct reference answers and 10
distinct current answers, ten matched prompts per side):
`overall_drift_detected = 0` in every cycle and no alert fired. This identity is the
pseudoreplication made visible: each cycle rebuilds its windows and recomputes its
permutations, but the reference window and the per cycle current answers are the
same fixed answer sets every time, so the numbers cannot change. Repeating cycles
against a deterministic target adds no statistical evidence, and nothing in this
artifact claims otherwise.

## What the artifacts say about determinism

Both providers behaved deterministically at the pinned settings in these runs, on
top of the earlier direct measurement (2026-11-07, on the database that existed
before the reruns: one distinct response text per panel prompt across 90 stored
Gemini responses). In the current artifacts, `current_unique_responses` is 10 in
every stored cycle against an unchanging reference of 12 distinct answers in 50
runs, and the pooled p values are identical to the sixteen digit precision in every
tested cycle. Groq's reference window has carried a different distinct text count
across recordings (15 on 2026-11-07, 12 on 2026-11-08), which shows the reference
side is not perfectly frozen across reruns, in a small way that the reported unique
counts make visible.

## Timing, kept separate by design

| Quantity | Value | Meaning |
| --- | --- | --- |
| Provider per-call latency | Groq p95 0.899 to 1.231 s, Gemini p95 6.531 s (single cycle run) to 15.381 s max (extended run) | Provider generation time per model call |
| End-to-end cycle wall time (single cycle Gemini run) | 7.204 s | Full Gemini cycle including generation, features, tests, storage |
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
5. HTTP 429 on extended cycle 1 during the first 2026-11-08 regeneration attempt:
   the preceding invocation of the single cycle comparison had completed two
   minutes earlier, and its ten Gemini calls were still inside the per model quota
   window, so the extended run's first burst was rejected with zero stored runs.
   Fix: at least 90 seconds between the end of any invocation that touched Gemini
   and the start of the next one. The final committed runs used that spacing and
   completed both stages with zero errors. The lesson generalizes the in-run
   pacing rule: consecutive experiment invocations stack against the same rolling
   window exactly as cycles inside one invocation do.

## Limitations

- Cross-provider confounding: models differ, so the comparison validates that the
  pipeline runs end to end against real providers and measures a cross-provider
  difference, not provider behavior in isolation and not within-model drift.
- Determinism bounds the statistics: both adapters ran at temperature 0, so the
  pooled windows describe two fixed answer sets rather than sampling from
  generative distributions. MMD now collapses duplicate embeddings, so its pooled
  windows hold 12 distinct reference points and 10 distinct current points (the
  unique counts are reported in the JSON), while KS and the validity proportion
  test deliberately keep every stored run (equal lengths from different texts are
  genuinely different observations, and a failed call is a real event regardless
  of repeats). Twelves points against ten are a calibrated comparison of two answer
  sets, not an assay of generative distributions; every identical p value across
  cycles is one comparison, not independent confirmation. The paired prompt level
  permutation respects the matched panel's dependence structure but still carries
  only ten exchangeable prompt units per side. The category family emits no p
  values at all for these windows.
- The flagged category was withdrawn: the 2026-11-07 recording marked refusal_probe
  BH significant at 0.020; recounted effective samples (four distinct reference
  observations versus two distinct current observations) showed the confidence was
  inflated by pseudoreplication, so the inference is retracted here and the gate
  and the MMD deduplication now prevent it structurally. The descriptive distance
  (MMD statistic 0.450, the largest of the five) stays in the narrative only.
- No detection-rate or false-alarm claims follow from any of this; the synthetic
  benchmarks remain the source for those metrics, and a within-model temporal
  comparison (one model, two versions) remains the missing demonstration for
  production drift claims.
- Slow tail latencies exist on the free tier: one 48 second call (2026-11-07) and
  one about 15 second call in the extended run (2026-11-08) succeeded without
  hitting the 60 second limit.
- Different token accounting: the two providers return different token fields and
  count differently, so token totals are not directly comparable.
- The quota-spacing rules matter beyond a single invocation: the first regeneration
  attempt on 2026-11-08 had its extended cycle 1 rejected with HTTP 429 because
  the prior invocation's Gemini calls were still inside the rolling window; the
  final committed runs waited 90 seconds between invocations and recorded zero
  errors.

## Reproducing

Set GROQ_API_KEY and GEMINI_API_KEY in the environment (or .env), then:

```
DRIFT_MONITOR_TRACKING=0 python -m experiments.real_providers
```

Optional flags: `--reference-cycles N` (default 5), `--current-cycles N` (a value
above 1 keeps the default two-cycle persistence rule and lets the category window
accumulate five cycles; the distinct observation gate still applies inside it),
`--resume` (extend an existing data/real_providers.db by skipping the Groq
reference collection; leave at least 90 seconds between invocations that touch
Gemini so the per model quota window clears),
`--out-prefix` (default `real_provider`), and `--out-dir`. The run deletes and
rebuilds `data/real_providers.db` unless resuming, so every fresh execution starts
from an empty history. Without the needed keys the script exits before any API
call; it never falls back to the fake provider.
