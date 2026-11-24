# Statistical validity audit: deployed 3:1 matched-prompt design

This document audits whether the production detector p-values are valid for the
sampling design actually deployed in `config/config.gemini.yaml`, and records
the benchmark used to decide whether any production change is justified.
Simulation results are explicitly synthetic and are **not** observations from
the hosted service.

## 1. Reconstructed sampling design

Source of truth: `config/config.gemini.yaml` (`reference.window_size: 120`,
`monitoring.batch_size: 40`), `render.yaml` (sets `DRIFT_MONITOR_CONFIG`), and
the runtime functions `complete_cycles_only()` /
`select_matched_windows()` in `monitor.py:211-252`.

- Each cycle probes the full 40-prompt panel (`config/canary_prompts.json`,
  version `3.0.0`) exactly once per prompt; `balanced_category_subset()` is the
  identity when `num_probes == 40`.
- `complete_cycles_only()` keeps only cycles with exactly one stored row per
  panel prompt ID. Failed and empty probes still produce rows and therefore do
  not make a cycle incomplete.
- `select_matched_windows()` takes the oldest 120 eligible rows as reference and
  the newest 40 eligible rows after them as current. With 40 rows per complete
  cycle this is **3 complete reference cycles and 1 complete current cycle**.
  The windows are disjoint, fixed by insertion order, and use the same prompt
  IDs on both sides.
- Confirmed in `data/seed_gemini.db`: 4 cycles of 40 rows, IDs 1-160; reference
  IDs 1-120, current IDs 121-160; every prompt has nominal reference count 3
  and current count 1. `code_018` has three failed reference rows and one
  successful current row, so the realized successful clusters are unbalanced
  (39 paired prompt clusters with embeddings; 117 reference and 40 current
  successful responses). The committed `test_results` row records
  `paired_cluster_prompt_pairs = 39`, consistent with this.

Consequences for inference: prompts are fixed and repeated across cycles, and
the realized per-prompt cluster sizes are unequal (`3:1` nominally, smaller
after failures). Neither pooled iid tests nor whole-cluster swaps are
automatically valid.

## 2. Audit of the current tests

### 2.1 `paired_cluster_mmd_test()` (`drift_tests.py:267-381`)

- Statistic: unbiased MMD² on RBF kernel with pooled median bandwidth.
- Pairing: prompts present on both sides; prompts with no successful response
  on one side are excluded.
- Null distribution: for each paired prompt, the **whole** reference response
  set and current response set are swapped or not (`2^P` patterns; exhaustively
  enumerated when `P <= 12`, otherwise a seeded random sample of 1000 patterns
  including identity). `p = #(stat >= observed) / #evaluated`.

**Validity condition.** Whole-cluster swapping is a valid permutation only when
the two clusters have the **same size** for every prompt. It then exchanges
prompt phases without changing how many observations sit on each side, so every
permuted assignment is a possible realization under the null of exchangeable
prompt phases.

**Why it fails at 3:1.** Swapping a prompt with 3 reference and 1 current
observation produces 1 reference and 3 current observations. That assignment
has different group sizes from the observed one, so it is not a realization of
the deployed design under the null; the reference distribution of MMD² is
computed under a different allocation. Two concrete pathologies follow:

1. MMD² is symmetric under exchange of its two groups, so the pattern that
   swaps *all* prompts returns exactly the observed statistic (with group roles
   reversed). The permutation null is forced to contain a duplicate of the
   observed value.
2. Partial swaps change the pooled sizes from 117:40 toward 40:117, changing the
   variance and the unbiased U-statistic terms used in the null reference
   distribution. A mean/variance shift can therefore be mis-read as a size
   artefact rather than as evidence against the null.

**Allocation check.** With `P = 39` matched prompts and a nominal `3:1`
cluster ratio, swapping `k` whole prompts leaves `3(P - k) + k = 3P - 2k`
reference rows and `P + 2k` current rows. The permuted allocation therefore
takes the values `(117, 39), (115, 41), …, (39, 117)` as `k` runs from 0 to 39,
while the observed allocation is always `(117, 39)`. Only `k = 0` and `k = P`
reproduce the observed sizes, and `k = P` returns the same MMD² with the group
roles reversed. The null reference distribution is thus a mixture over group
sizes the deployed design never produces; it is not a valid null for the
observed statistic.

This is **not** covered by the existing equal-cluster unit tests in
`tests/test_drift_tests.py`, which use five reference and five current copies
per prompt.

### 2.2 Pooled KS on response lengths (`monitor.py:1192-1201`, `drift_tests.py:6-25`)

`ks_2samp` assumes independent observations within each group. The deployed
data contain three repeated lengths per prompt in reference and one in current,
with substantive prompt baselines (`prompt_len_std` in the benchmark). This is
positive intra-prompt correlation: pooled KS treats repeated measurements of
the same prompt as independent evidence, so its effective sample size is
overstated. Missingness is also informative: failures remove length/embedding
observations but remain in the availability test.

### 2.3 Fisher / two-proportion z on availability (`drift_tests.py:27-98`, `monitor.py:1203-1212`)

Validity flags are pooled across prompts (`include_failed=True`, so failed and
empty probes count as misses). The test assumes independent Bernoulli draws
with a common rate in each window. In reality each prompt has its own success
probability, repeated across three reference cycles, and at least one prompt
is failure-prone. This produces both prompt heterogeneity (overdispersion) and
within-prompt dependence; the pooled binomial variance is too small.

### 2.4 Family and alert policy

- `monitor.py:1315-1350`: Benjamini-Hochberg at `alpha = 0.05` on the three
  p-values (KS, availability, MMD). The three tests share the same repeated
  prompt data, so their dependence must be considered in the any-detector
  false-positive rate.
- `monitor.py:1595-1639`: drift alerts require a current-cycle distribution
  trigger (KS or MMD significant) **and** two consecutive stored
  `overall_drift_detected` flags. The stored flag is "any corrected test",
  so an availability-only rejection in the prior cycle can satisfy the streak.
  This is documented in section 5 of the alert-policy trace and is relevant to
  the alert-level false alarms measured below.
- Quality alerts from availability are not persistence-gated.

## 3. Candidate methods evaluated

All candidates preserve the deployed pairing and use the same BH family and
alert gate. Their null hypotheses and assumptions are:

| Test | Null | Exchangeability / independence required | Fit to 3:1 design |
| --- | --- | --- | --- |
| Current whole-cluster MMD | Joint distribution invariant to swapping prompt phases | Each prompt's reference and current response sets equal size and exchangeable | No: swap changes group sizes |
| Within-prompt allocation MMD | Within each prompt, cycle labels are exchangeable across the available observations | Conditional on prompt identity, the 3:1 label assignment is random; possible temporal dependence is a limitation | Yes: preserves 3:1 allocation per prompt |
| Pooled KS lengths | Reference and current lengths iid from same distribution | Independent observations within window | No: repeats prompts |
| Paired mean t lengths | Mean per-prompt length difference is zero | Prompt-level differences approximately normal / 40 prompts for CLT; prompts independent | Yes: one difference per prompt |
| Pooled Fisher/z availability | Common availability probability in both windows | Independent Bernoulli observations | No: repeats prompts with heterogeneous rates |
| Within-prompt availability permutation | Within each prompt, validity labels exchangeable across cycles | Same 3:1 exchangeability as allocation MMD; temporal dependence is a limitation | Yes: preserves allocation and prompt-specific rates |

The paired candidates are deliberately minimal: they keep the existing kernel,
BH correction, API field names/types, and alert policy; they only replace the
permutation scheme or pool the data at the prompt level before testing. The
legacy `ks_*` and `proportion_z_*` field names are retained for compatibility
even though the inferential values now come from a paired t statistic and a
within-prompt permutation; the original pooled values are stored alongside
them under `*_pooled` keys so effect sizes remain inspectable. The API response
schema itself is unchanged.

## 4. Benchmark design

Script: `experiments/sampling_design_calibration.py`, fixed seeds, outputs in
`results/statistical_audit/`. Each simulated trial has 40 prompts, 3 reference
cycles and 1 current cycle, prompt-specific embedding and length baselines,
within-prompt noise, a failure-prone prompt with 25% availability, and optional
AR(1) temporal correlation. Conditions:

- **A** null (`rho = 0, 0.30, 0.60`)
- **B** length mean shift (0.3, 0.6, 1.2 of within-prompt SD)
- **C** semantic mean shift (0.25, 0.5, 1.0 multiples of the within-prompt
  embedding noise norm along a random direction)
- **D** semantic variance/shape shift (1.2x, 1.5x, 2.0x)
- **E** availability drop (0.05, 0.15, 0.25 absolute)
- **F** combined shift (two magnitudes)

Every cell reports rejection counts, rates, and Wilson 95% intervals. Alert
simulations use the deployed two-consecutive-cycle gate: a distribution
trigger in the current cycle plus two consecutive any-BH flags. Detection delay
is reported only with trial counts and uncertainty; no rate is reported as
100% without its interval.

## 5. Results

All numbers below are **synthetic Monte Carlo estimates**, not observations
from the hosted service. The calibration run used 1000 null trials per
correlation level, 400 trials per power cell, and 200 alert sequences, with
fixed seeds; every rate is shown as a percentage with a Wilson 95% interval
and the rejection count over trials. Full tables and plots are in
`results/statistical_audit/` (`SUMMARY.md`, `null_type_i.csv`, `power.csv`,
`alert_rates.csv`, `detection_delay.csv`, and five PNGs).

### 5.1 Null type-I error (BH-adjusted flags, alpha = 0.05)

| rho | method | KS | availability | MMD | any | dist trigger | ms/window |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0.00 | current | 0.1% [0.0, 0.6] (1/1000) | 1.9% [1.2, 2.9] (19/1000) | 0.2% [0.1, 0.7] (2/1000) | 0.9% [0.5, 1.7] (9/1000) | 0.2% [0.1, 0.7] (2/1000) | 74.3 |
| 0.00 | paired | 5.4% [4.2, 7.0] (54/1000) | 2.4% [1.6, 3.5] (24/1000) | 5.6% [4.3, 7.2] (56/1000) | 4.5% [3.4, 6.0] (45/1000) | 3.5% [2.5, 4.8] (35/1000) | 31.1 |
| 0.30 | current | 0.1% [0.0, 0.6] (1/1000) | 1.9% [1.2, 2.9] (19/1000) | 0.5% [0.2, 1.2] (5/1000) | 0.8% [0.4, 1.6] (8/1000) | 0.1% [0.0, 0.6] (1/1000) | 74.3 |
| 0.30 | paired | 4.9% [3.7, 6.4] (49/1000) | 2.4% [1.6, 3.5] (24/1000) | 6.9% [5.5, 8.6] (69/1000) | 4.6% [3.5, 6.1] (46/1000) | 3.7% [2.7, 5.1] (37/1000) | 31.2 |
| 0.60 | current | 0.2% [0.1, 0.7] (2/1000) | 1.9% [1.2, 2.9] (19/1000) | 1.3% [0.8, 2.2] (13/1000) | 0.9% [0.5, 1.7] (9/1000) | 0.2% [0.1, 0.7] (2/1000) | 73.1 |
| 0.60 | paired | 4.5% [3.4, 6.0] (45/1000) | 2.4% [1.6, 3.5] (24/1000) | 6.3% [5.0, 8.0] (63/1000) | 5.0% [3.8, 6.5] (50/1000) | 4.0% [3.0, 5.4] (40/1000) | 30.7 |

The current family is **strongly conservative** (any-detector 0.8-0.9% rather
than 5%): the invalid whole-cluster swap and the pooled, pseudoreplicated
tests do not produce valid nominal p-values, though the observed error here is
in the safe direction. The paired family is close to nominal for the any and
distribution triggers at all three correlation levels. Its MMD leg runs
slightly high under temporal correlation (6.9% [5.5, 8.6] at rho = 0.30), the
expected cost of assuming within-prompt exchangeability when cycles are
AR(1)-correlated; this is a limitation, not a hidden exactness claim. The
availability permutation is conservative (2.4%) because its null
distribution is discrete with one current observation per prompt.

### 5.2 Power by shift condition (any-detector BH flag)

| Condition | Magnitude | current | paired | Driving leg (current -> paired) |
| --- | --- | --- | --- | --- |
| B length | 0.3 | 3.0% [1.7, 5.2] (12/400) | 68.0% [63.3, 72.4] (272/400) | KS 7.8% -> 82.5% |
| B length | 0.6 | 43.2% [38.5, 48.1] (173/400) | 99.8% [98.6, 100.0] (399/400) | KS 67.5% -> 100.0% (400/400) |
| B length | 1.2 | 100.0% [99.0, 100.0] (400/400) | 100.0% [99.0, 100.0] (400/400) | KS both 100.0% (400/400) |
| C mean | 0.25 | 79.2% [75.0, 82.9] (317/400) | 97.8% [95.8, 98.8] (391/400) | MMD 89.0% -> 99.2% |
| C mean | 0.5 | 100.0% [99.0, 100.0] (400/400) | 100.0% [99.0, 100.0] (400/400) | MMD both 100.0% (400/400) |
| C mean | 1.0 | 100.0% [99.0, 100.0] (400/400) | 100.0% [99.0, 100.0] (400/400) | MMD both 100.0% (400/400) |
| D shape | 1.2 | 1.2% [0.5, 2.9] (5/400) | 7.0% [4.9, 9.9] (28/400) | MMD 0.0% -> 7.0% |
| D shape | 1.5 | 1.2% [0.5, 2.9] (5/400) | 9.2% [6.8, 12.5] (37/400) | MMD 0.8% -> 11.2% |
| D shape | 2.0 | 1.2% [0.5, 2.9] (5/400) | 11.5% [8.7, 15.0] (46/400) | MMD 1.0% -> 18.2% |
| E availability | 0.05 | 6.8% [4.7, 9.6] (27/400) | 13.2% [10.3, 16.9] (53/400) | prop 14.5% -> 16.0% |
| E availability | 0.15 | 54.5% [49.6, 59.3] (218/400) | 63.5% [58.7, 68.1] (254/400) | prop 72.8% -> 76.5% |
| E availability | 0.25 | 92.8% [89.8, 94.9] (371/400) | 93.8% [90.9, 95.7] (375/400) | prop 96.8% -> 97.0% |
| F combined | 0.5 | 100.0% [99.0, 100.0] (400/400) | 100.0% [99.0, 100.0] (400/400) | MMD 99.8% -> 100.0% |
| F combined | 1.0 | 100.0% [99.0, 100.0] (400/400) | 100.0% [99.0, 100.0] (400/400) | MMD both 100.0% (400/400) |

The paired family's largest gain is on length shifts: the pooled KS misses a
0.3-SD shift almost entirely (3.0% [1.7, 5.2]) while the paired t detects it in
68.0% [63.3, 72.4] of trials. Semantic mean shifts and combined shifts are
detected by both methods once large enough; the paired MMD keeps a modest edge
at the smallest mean shift. Availability power is similar because both legs
see the same failed/empty rows. **Shape-only shifts remain weak** (11.5%
[8.7, 15.0] at 2.0x variance even for the paired method): a single current
observation per prompt cannot support strong shape inference, and neither
method should be advertised as a shape detector.

### 5.3 Two-cycle alert policy

| Condition | rho | current | paired |
| --- | --- | --- | --- |
| Null false alarms | 0.00 | 0.0% [0.0, 1.9] (0/200) | 1.5% [0.5, 4.3] (3/200) |
| Null false alarms | 0.30 | 0.0% [0.0, 1.9] (0/200) | 0.5% [0.1, 2.8] (1/200) |
| Combined-shift detection | 0.00 | 81.5% [75.5, 86.3] (163/200) | 100.0% [98.1, 100.0] (200/200) |
| Combined-shift detection | 0.30 | 77.0% [70.7, 82.3] (154/200) | 99.0% [96.4, 99.7] (198/200) |

Detection delay among detected sequences averaged 2.0 cycles for both
methods, which is the minimum permitted by the two-consecutive-cycle gate;
the benchmark does not report a confidence interval for the delay mean itself,
only for the detection rate. Alert-level false alarms are conservative for
both methods. No alert rate is reported without its trial count and interval.

### 5.4 Computational cost

Measured per simulated window on one core: the current family takes 51-80 ms
and the paired family 24-34 ms. The paired MMD is faster despite evaluating
1000 within-prompt patterns because it batches the unbiased MMD² calculation
into matrix products, whereas the legacy whole-cluster test loops over
patterns in Python. The paired length and availability tests are linear in the
number of prompts and negligible next to the kernel matrix.

## 6. Decision and production impact

**A concrete issue was demonstrated before any detector was changed.** The
whole-cluster MMD permutation is not a valid null for unequal prompt clusters:
under the deployed 3:1 allocation its permuted group sizes range from
`(117, 39)` to `(39, 117)` while the observed statistic is always `(117, 39)`
(section 2.1), and an independent synthetic check with 5:1 clusters rejected
about 15.7% of null trials at nominal alpha 0.05 versus about 3.3% for
balanced 3:3 clusters. The pooled KS and availability tests also ignore the
repeated-panel structure; in the calibration this made them conservative and
cost the family most of its length-shift power (section 5.2). Thresholds and
the dashboard were not touched.

**The adopted change is minimal.** `drift_tests.py` adds three functions:
`allocation_preserving_mmd_test` (within-prompt randomization at fixed 3:1
allocation, same RBF kernel and median bandwidth, exhaustive up to 4096
patterns then 1000 seeded samples), `paired_mean_length_test` (t on per-prompt
mean length differences), and `paired_validity_permutation_test` (within-prompt
randomization of validity). `monitor.py` uses these for the three inferential
p-values in the existing BH family and the unchanged two-cycle alert gate, and
stores the former pooled results under `*_pooled` keys as exploratory effect
sizes. The API response model, field names/types, stored schema, and detector
thresholds are unchanged.

**Why this alternative and not a more elaborate model.** It is the smallest
change that restores the design's exchangeability unit. It retains the existing
kernel, statistic, correction, and alert semantics, and needs no new tuning
parameters. Per-candidate assumptions are in section 3; the honest limitations
are: (i) within-prompt exchangeability across cycles is an assumption, and
AR(1) correlation pushes the paired MMD null to 6.3-6.9% in simulation; (ii)
the availability permutation is discrete and conservative with one observation
per prompt; (iii) shape shifts are detected only weakly; (iv) all evidence is
synthetic and does not guarantee hosted-service behaviour.

**Historical data.** No stored rows or records were modified. Section 7 lists
which derived columns still describe real data and which must be regenerated
because their p-values and flags came from the old algorithms.

## 7. Historical metric validity

If a detector statistic or p-value algorithm changes, the following stay valid
because they derive from stored raw data or independent arithmetic:

- `probe_runs` rows (text, length, validity, status, prompt ID, cycle ID),
  `response_features`, and stored embeddings, provided the embedding model and
  preprocessing are unchanged.
- Window sizes, timestamps, cycle links, baseline manifest, provider latency,
  token counts, unique-response counts, length-shift percentage, refusal rates,
  and absolute invalid-share guardrail.

The following must be regenerated or re-derived, because changing one p-value
changes ranks and flags across the shared BH family:

- All `*_statistic`, `*_raw_pvalue`, `*_adjusted_pvalue`, and `*_significant`
  columns in `test_results`, including tests whose own algorithm did not change.
- `bh_significant_tests`, `overall_drift_detected`, API-derived
  `drift_detected` / `distribution_shift_detected` / `quality_change_detected`
  / `flagged_tests`, and any persisted `alerts` rows (their p-values and streak
  text are snapshots, and the streak reads stored flag history).
- Category p-values and flags if the MMD statistic changes, plus MLflow metrics
  and experiment artifacts derived from the same results.

There is no general recompute utility in the repository; raw inputs are stored,
so an offline recompute is possible but would need to be written.

## 8. Reproduction

```bash
# smoke run (200 null / 60 power / 40 alert-sequence trials, ~4 min)
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python experiments/sampling_design_calibration.py --quick

# calibration-grade run used for the numbers in section 5
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
  python experiments/sampling_design_calibration.py \
  --trials 1000 --power-trials 400 --sequences 200

# turn the generated CSVs into results/statistical_audit/SUMMARY.md
python experiments/summarize_statistical_audit.py

ruff check .
python -m pytest -q
node web/smoke.test.js
```

The thread limits matter: without them, NumPy's BLAS spawns a thread per core
for the small kernel matrices and the benchmark spends most of its time on
thread overhead rather than arithmetic.
