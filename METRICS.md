# Metrics Reference

Every metric this project computes and every headline number it has measured, in one
place. Each section names the committed CSV or database table the numbers come from.
All experiment scripts take a `--seed`, so every table here can be regenerated. These
are per-cycle frequencies measured under synthetic nulls and injected shifts, not
long-run production guarantees.

## Fixed configuration behind every number

| Setting | Value |
| --- | --- |
| Significance threshold (alpha) | 0.05 |
| Pooled test family | 3 detectors (KS, proportion, MMD) |
| Within-cycle correction | Benjamini-Hochberg |
| Probe panel per cycle | 10 prompts, 2 per category, 5 categories |
| Headline reference window | 50 runs, matched prompt ids |
| Category window | 5 cycles, 10 samples per category, min 8 to test |
| MMD permutations (runtime) | 500, so minimum attainable p is about 1/501 |
| MMD bandwidth | median heuristic on the pooled window |
| Embedding models tested | all-MiniLM-L6-v2 (384 dim), all-mpnet-base-v2 (768 dim) |
| Alert persistence | flag must hold 2 consecutive cycles |
| Alpha spending | off by default, horizon 30 cycles, Holm within cycle |

## 1. False alarm calibration under the null

Source: `results/calibration_fpr.csv`, 1,000 synthetic null simulations. Raw per
simulation p values and BH adjusted values for all three detectors are in
`results/calibration_pvalues.csv` (1,000 rows, one per simulation).

| Test | Raw FPR | FPR after BH | Mean raw p |
| --- | --- | --- | --- |
| KS (lengths) | 0.037 | 0.011 | 0.6058 |
| Proportion (validity) | 0.040 | 0.006 | 0.4895 |
| MMD (embeddings) | 0.050 | 0.003 | 0.4994 |
| Any detector | 0.121 | 0.019 | n/a |

Nominal alpha is 0.05. Every raw rate sits at or below it, and the BH corrected
any-detector rate is 0.019 per cycle.

## 2. Detection delay and alert policies, full deployed pipeline

Source: `results/delay_detection_delay.csv` and `results/delay_false_alarms.csv`,
100 Monte Carlo trials over a 90 day horizon with the shift injected on day 45. The
per-day decision reproduces the deployed pipeline: three detectors, BH across them,
then the alert rule. Shift magnitude 1.2 standard deviations on lengths, valid
fraction dropped from 0.95 to 0.70, and the same magnitude on embeddings.

| Rule | Mean delay (days) | Median delay (days) | Detection rate | False alarms per 100 quiet days | Mean false alarms in 45 quiet days |
| --- | --- | --- | --- | --- | --- |
| Single pooled cycle (unfiltered) | 0.00 | 0.0 | 100.0% | 3.42 | 1.54 |
| k-of-n (2 of last 5) | 0.88 | 1.0 | 100.0% | 1.76 | 0.79 |
| Deployed persistence (2 consecutive) | 0.97 | 1.0 | 100.0% | 0.20 | 0.09 |
| CUSUM (h = 3.5) | 0.05 | 0.0 | 100.0% | 3.76 | 1.69 |

The deployed rule trades about one day of delay for a seventeen-fold quieter baseline
against the unfiltered flag. Per-day trajectories of the first trial (p values, pooled
flag, all rule alerts) are in `results/delay_alerts.csv`.

## 3. Localization at production sample size

Source: `results/localization_results.csv`, 100 trials per shift, 10 samples per
category (production parity: 5 cycles of 2 prompts per category). One category is
shifted along a random direction. The procedure is the same `category_mmd_tests` the
monitor runs at runtime. Detection rate by category:

| Shift (sigma) | factual | reasoning | code (shifted) | refusal | formatting |
| --- | --- | --- | --- | --- | --- |
| 0.5 | 0.01 | 0.00 | 0.01 | 0.01 | 0.00 |
| 1.0 | 0.01 | 0.00 | 0.01 | 0.01 | 0.00 |
| 1.5 | 0.01 | 0.00 | 0.03 | 0.01 | 0.00 |
| 2.0 | 0.01 | 0.00 | 0.07 | 0.01 | 0.00 |
| 3.0 | 0.01 | 0.00 | 0.20 | 0.01 | 0.00 |

Summary metrics per shift (precision of the flagged set, recall on the shifted
category, F1, and the null trial any-category flag rate):

| Shift (sigma) | Precision | Recall | F1 | Null flag rate |
| --- | --- | --- | --- | --- |
| 0.5 | 0.010 | 0.010 | 0.010 | 0.03 |
| 1.0 | 0.010 | 0.010 | 0.010 | 0.03 |
| 1.5 | 0.030 | 0.030 | 0.030 | 0.03 |
| 2.0 | 0.070 | 0.070 | 0.070 | 0.03 |
| 3.0 | 0.195 | 0.200 | 0.1975 | 0.03 |

The honest reading: with ten samples per category, category-wise MMD localization is
weak even for large shifts. At production sample sizes the pooled tests carry the
detection load, and the category pointer is best-effort evidence for a person, not a
verdict. Widening `category_window_cycles` is the configuration knob that improves it.

## 4. Detector baselines

Source: `results/baselines_power.csv`, 200 simulations per cell, 50 reference and 10
current samples. Power of each detector by shift size in standard deviations:

| Shift | KS power (length shift) | MMD power (mean shift) | Centroid power (mean shift) | Centroid power (variance shift) |
| --- | --- | --- | --- | --- |
| 0.0 | 0.00 | 0.00 | 0.00 | 0.01 |
| 0.2 | 0.00 | 0.00 | 0.00 | 0.01 |
| 0.4 | 0.15 | 1.00 | 0.00 | 0.01 |

This is why MMD is the headline detector: at a 0.4 sigma mean shift with 10 current
samples it detects everything while KS catches 15% and the centroid baseline nothing.
Under the null, all three sit at their nominal rates.

## 5. Power curves: samples needed per shift

Source: `results/power_curves.csv`, 50 simulations per cell, alpha 0.05, shift in
standard deviations of the length scale, n is the current window size.

KS test power:

| n | delta 0.0 | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | 0.04 | 0.00 | 0.24 | 0.60 | 0.82 | 1.00 |
| 25 | 0.04 | 0.20 | 0.76 | 0.96 | 1.00 | 1.00 |
| 50 | 0.02 | 0.46 | 0.96 | 1.00 | 1.00 | 1.00 |
| 100 | 0.04 | 0.82 | 1.00 | 1.00 | 1.00 | 1.00 |

Proportion test power (valid fraction shift):

| n | delta 0.0 | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | 0.00 | 0.02 | 0.06 | 0.22 | 0.30 | 0.64 |
| 25 | 0.02 | 0.10 | 0.24 | 0.56 | 0.72 | 0.98 |
| 50 | 0.02 | 0.14 | 0.64 | 0.90 | 0.98 | 1.00 |
| 100 | 0.02 | 0.34 | 0.84 | 0.98 | 1.00 | 1.00 |

MMD power (embedding mean shift):

| n | delta 0.0 | 0.5 | 1.0 | 1.5 | 2.0 | 3.0 |
| --- | --- | --- | --- | --- | --- | --- |
| 10 | 0.02 | 0.60 | 1.00 | 1.00 | 1.00 | 1.00 |
| 25 | 0.06 | 0.98 | 1.00 | 1.00 | 1.00 | 1.00 |
| 50 | 0.02 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| 100 | 0.04 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

Practical reading: MMD at the production panel size of 10 already has 0.60 power at a
0.5 sigma embedding shift, while KS needs about 50 samples for the same on lengths.

## 6. MMD sensitivity and bandwidth ablation

Source: `results/sensitivity.csv`, 25 trials per cell, alpha 0.05, a fixed injected
embedding shift. Power is 1.0 everywhere, so the ablation is about how the bandwidth
changes the effect size the test sees, not whether it detects.

| Model | Dim | Bandwidth | Permutations | Power | Mean p | Mean MMD stat |
| --- | --- | --- | --- | --- | --- | --- |
| all-MiniLM-L6-v2 | 384 | 0.5x median | 200 | 1.0 | 0.00498 | 0.25475 |
| all-MiniLM-L6-v2 | 384 | 0.5x median | 1000 | 1.0 | 0.001 | 0.25475 |
| all-MiniLM-L6-v2 | 384 | median | 200 | 1.0 | 0.00498 | 0.25257 |
| all-MiniLM-L6-v2 | 384 | median | 1000 | 1.0 | 0.001 | 0.25257 |
| all-MiniLM-L6-v2 | 384 | 2.0x median | 200 | 1.0 | 0.00498 | 0.15001 |
| all-MiniLM-L6-v2 | 384 | 2.0x median | 1000 | 1.0 | 0.001 | 0.15001 |
| all-MiniLM-L6-v2 | 384 | fixed 1.0 | 200 | 1.0 | 0.00498 | 0.2838 |
| all-MiniLM-L6-v2 | 384 | fixed 1.0 | 1000 | 1.0 | 0.001 | 0.2838 |
| all-mpnet-base-v2 | 768 | 0.5x median | 200 | 1.0 | 0.00498 | 0.25607 |
| all-mpnet-base-v2 | 768 | 0.5x median | 1000 | 1.0 | 0.001 | 0.25607 |
| all-mpnet-base-v2 | 768 | median | 200 | 1.0 | 0.00498 | 0.25325 |
| all-mpnet-base-v2 | 768 | median | 1000 | 1.0 | 0.001 | 0.25325 |
| all-mpnet-base-v2 | 768 | 2.0x median | 200 | 1.0 | 0.00498 | 0.15029 |
| all-mpnet-base-v2 | 768 | 2.0x median | 1000 | 1.0 | 0.001 | 0.15029 |
| all-mpnet-base-v2 | 768 | fixed 1.0 | 200 | 1.0 | 0.00498 | 0.28493 |
| all-mpnet-base-v2 | 768 | fixed 1.0 | 1000 | 1.0 | 0.001 | 0.28493 |

Doubling the bandwidth shrinks the measured MMD statistic by about 40%, which is why
the bandwidth is computed with the median heuristic on every window instead of fixed.

## 7. Drift as a proxy for quality regression

Source: `results/reliability_results.csv`, 100 trials per cell, production sample
sizes (50 reference, 10 current), equal mixture over four conditions: null, benign
distribution shift only, silent quality regression only, and both. Degradation means
the valid fraction drops from 0.95 to 0.70; distribution shift means embeddings move
1.5 sigma while valid-answer embeddings stay fixed, matching what the monitor's MMD
windows actually contain. Laplace smoothing (add one success, add two total) applies
to every conditional probability.

Detection rate per condition per signal:

| Condition | KS | MMD | Validity | Distributional (BH) | Pooled (BH) |
| --- | --- | --- | --- | --- | --- |
| Null | 0.07 | 0.01 | 0.00 | 0.02 | 0.01 |
| Drift only (benign) | 0.18 | 1.00 | 0.03 | 1.00 | 1.00 |
| Quality only (silent) | 0.08 | 0.06 | 0.61 | 0.03 | 0.64 |
| Both | 0.26 | 1.00 | 0.69 | 1.00 | 1.00 |

Proxy quality of each flag on the equal mixture:

| Signal | P(degradation \| flag) | P(flag \| degradation) | Benign flag rate |
| --- | --- | --- | --- |
| KS | 0.55 | 0.16 | 0.13 |
| MMD | 0.47 | 0.49 | 0.55 |
| Validity (behavioral) | 0.97 | 0.64 | 0.02 |
| Distributional family (BH) | 0.46 | 0.47 | 0.55 |
| Pooled pipeline (BH) | 0.59 | 0.80 | 0.55 |

Reliability curve: P(degradation | flag) as the base rate of benign shifts
(`pi_benign`) rises from 0 to 0.4, with quality_only and both fixed at 0.2 each:

| Benign base rate | Distributional family | Pooled pipeline | Behavioral only |
| --- | --- | --- | --- |
| 0.0 | 0.89 | 0.94 | 0.92 |
| 0.1 | 0.64 | 0.75 | 0.96 |
| 0.2 | 0.62 | 0.70 | 0.97 |
| 0.3 | 0.52 | 0.62 | 0.97 |
| 0.4 | 0.35 | 0.49 | 0.97 |

The three findings, stated plainly:

1. The signal families are nearly disjoint. A silent quality regression (the kind the
   MMD windows cannot see, because they only contain valid answers) is caught by the
   behavioral test 61% of the time and by the distributional family 3%. A benign
   distribution shift is caught 100% versus 3%.
2. On an equal mixture, a drift flag is a weak quality proxy on its own: the
   distributional family's flag predicts degradation at 0.46, barely above a coin
   flip, while the behavioral flag sits at 0.97.
3. The value of combining both is recall, not precision: the pooled pipeline detects
   80% of degradations against 64% behavioral alone and 47% distributional alone,
   while proxy precision stays dominated by the behavioral signal. And flag precision
   degrades as benign shifts become common, which is the operational condition to
   watch: in an environment that changes topics often, a drift flag means "look",
   not "broken".

## 8. Metrics computed per monitoring cycle (runtime)

Every cycle stores and logs these. Names below are the exact keys in the stored
result, the cycle report, and MLflow.

Detector statistics and p values:

| Metric | Meaning |
| --- | --- |
| `ks_statistic`, `ks_pvalue` | KS statistic and p on response length distributions |
| `proportion_z_stat`, `proportion_z_pvalue` | Validity proportion test; `proportion_test_method` records fisher when expected counts are small |
| `mmd_statistic`, `mmd_pvalue` | RBF MMD statistic and permutation p on embeddings |
| `ks_adjusted_pvalue`, `proportion_z_adjusted_pvalue`, `mmd_adjusted_pvalue` | BH adjusted values for the pooled family |
| `ks_significant`, `proportion_z_significant`, `mmd_significant` | 0/1 flags after correction |
| `bh_significant_tests` | Count of corrected rejections in the cycle |
| `overall_drift_detected` | Pooled flag for the cycle |
| `overall_quality_degradation` | Directional judgment that the change is a degradation |
| `centroid_distance` | Euclidean distance between reference and current embedding centroids, the baseline detector |
| `length_shift_pct` | Percent change in mean response length |
| `refusal_rate_change` | Change in refusal rate between windows |

Alert record effect sizes:

| Metric | Meaning |
| --- | --- |
| `tests_fired` | Which corrected tests rejected |
| `streak` | Consecutive flagged cycles when the alert fired (minimum 2) |
| `mmd_stat` | MMD statistic at alert time |
| `centroid_distance` | Centroid baseline at alert time |
| `length_shift_pct` | Length change at alert time |
| `affected_categories` | Categories flagged by the category-wise family |

Category-wise family (wider window, own BH correction):

| Metric | Meaning |
| --- | --- |
| `cat_<name>_pvalue` | Raw permutation MMD p for the category |
| `cat_<name>_adj_pvalue` | BH adjusted p within the category family |
| `cat_<name>_statistic` | Per category MMD statistic |
| skip reason | Categories under 8 samples per side are reported as skipped with counts, never tested |

Operational metrics:

| Metric | Meaning |
| --- | --- |
| `mean_latency`, `latency_p50`, `latency_p95` | Per model call latencies, each call timed individually |
| `probe_runs`, `flagged_probes`, `token_counts` | Cycle volume and validity counters |
| `reference_size`, `current_size` | Window sizes used by the tests |

Behavioral features stored per response (in `response_features`):

| Metric | Meaning |
| --- | --- |
| `is_refusal` | Rule based refusal marker hit |
| `repetition_ratio` | Share of repeated tokens in the answer |
| `token_count` | Token count estimate |
| `has_code_block` | Markdown code fence present |
| `response_length`, `is_valid` | Character length and validity flag |

## 10. The committed demo database

Source: `data/seed_drift.db`. Era 1 is 50 runs under adapter seed 42 (null), era 2 is
50 runs under seed 185 (shifted), 100 runs and 5 cycles total, matched panels
throughout.

Stored cycle results (in id order, the storage chronology):

| Cycle | Era | KS stat | KS p | KS adj p | MMD stat | MMD p | MMD adj p | Proportion adj p | KS sig | MMD sig |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | null | 0.5 | 0.0224 | 0.0336 | 0.3247 | 0.001996 | 0.005988 | 1.0 | 1 | 1 |
| 2 | null | 0.4 | 0.1150 | 0.1726 | 0.2879 | 0.005988 | 0.017964 | 1.0 | 0 | 1 |
| 3 | shift | 0.5 | 0.0224 | 0.0336 | 0.3247 | 0.001996 | 0.005988 | 1.0 | 1 | 1 |
| 4 | shift | 0.5 | 0.0224 | 0.0336 | 0.2918 | 0.005988 | 0.017964 | 1.0 | 1 | 1 |
| 5 | shift | 0.4 | 0.1150 | 0.1726 | 0.2679 | 0.009980 | 0.029940 | 1.0 | 0 | 1 |

Four drift alerts fire, all with streak 2 (the first flagged cycle stays silent under
the persistence rule), all severity medium, all with the MMD test among the fired
tests, and MMD adjusted p values 0.0180, 0.0060, 0.0180 and 0.0299. Behavioral
features over the 100 stored responses: 0 refusals, 0 code blocks, mean repetition
ratio 0.0393, mean token count 11.33. The proportion test shows nothing because the
seeded shift changes meaning and length, not validity, which is the intended
complementarity of the three detectors.

## What these numbers do not claim

- All rates are per-cycle frequencies under synthetic nulls and injected shifts.
- Separate BH corrections in the pooled family and the category family do not add up
  to one global false-alarm guarantee across families.
- The persistence rule is an operational alert filter, not a formal sequential
  procedure. The optional alpha-spending gate (off by default) is O'Brien Fleming in
  spirit, holds family-wise error over its declared 30 cycle horizon with Holm within
  the cycle, and cannot go below the permutation floor of about 1/501.
- Localization at production sample size is weak (section 3) and is presented as
  diagnostic evidence, not a verified localization capability.
- In the reliability study, quality regression is modeled as a validity drop with
  valid-answer embeddings held fixed. Real regressions can also shift valid-answer
  content, which the distributional family would see; the disjointness measured here
  is the clean case, not the worst case for drift flags.
