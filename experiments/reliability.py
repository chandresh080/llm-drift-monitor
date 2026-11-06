"""When is behavioral drift a reliable proxy for invalid-response regression?

This is the central research question of the project, studied directly. The
monitor produces two independent signal families per cycle: distributional
signals (KS on response lengths, MMD on embeddings) that ask "did the answer
distribution change", and a behavioral signal (the validity or refusal
proportion test, backed by refusal and repetition features) that asks "did the
invalid-response rate go up". This experiment measures how the two families
relate, and the scope is deliberate: degradation here means an invalid-response
regression, not general answer correctness, which this project does not measure.

Four conditions are simulated, the full 2x2 of distribution shift and
invalid-response regression:

- null: nothing changes.
- drift_only: embeddings shift (a benign topic or style change), validity fixed.
- invalid_only: the valid fraction drops, embeddings of the remaining valid
  answers unchanged. This is a silent regression: the monitor's MMD windows
  only contain valid runs, so by construction the distributional tests are
  blind to it.
- both: embedding shift and validity drop together, as a real model regression
  would often arrive.

Trials are stratified: exactly n_trials per condition, so every cell of every
table has the same sample size. Data handling matches the runtime: validity
covers all 10 responses, lengths and embeddings only the successful ones, and
the distributional detectors see a Binomial(10, p_valid) sized window.

Three kinds of numbers come out:

1. Detection rate per condition per signal, the ablation of the deployed
   pipeline into its parts.
2. Proxy quality of a drift flag: P(regression | flag), P(flag | regression)
   and the benign flag rate P(flag | no regression), per signal family. These
   are conditional on the simulated mixture; they are not universal precision
   estimates, which is why the base-rate sweep below matters.
3. A base-rate sensitivity curve: how the proxy precision of each family
   changes as the prevalence of benign distribution shifts rises. The value of
   a drift flag is a property of the environment, not just the detector.

The honest findings, confirmed in results: distributional flags are not a
reliable regression proxy on their own, the behavioral signal is precise for
invalid-response regressions but blind to pure distribution change, and the
deployed combination buys recall at some cost in proxy precision.
"""
import argparse
import csv
import os
import matplotlib.pyplot as plt
import numpy as np

from drift_tests import (ks_test, proportion_test, mmd_test,
                         benjamini_hochberg_correction)
from experiments.sim import (make_lengths, make_validity, make_embeddings,
                             shift_lengths, shift_validity, shift_embeddings,
                             DEFAULT_LENGTH_STD)

REF_N = 50
CUR_N = 10
DEGRADED_P_VALID = 0.70
DEFAULT_DELTA = 1.5
LENGTH_DELTA = 0.5  # accompanying length change in the drift conditions
MMD_PERMUTATIONS = 500  # matches runtime
CONDITIONS = ['null', 'drift_only', 'invalid_only', 'both']
SIGNALS = ['ks', 'mmd', 'validity', 'dist_bh', 'pooled_bh']
DISTRIBUTIONAL = ['ks', 'mmd']


def run_trial(condition: str, trial_seed: int, delta: float,
              ref_lengths, ref_validity, ref_embeddings) -> dict:
    """Simulate one current window under the condition and run every signal.

    Data handling matches the runtime: validity is drawn for all CUR_N
    responses, lengths and embeddings only for the successful ones.
    """
    rng = np.random.default_rng(trial_seed)
    cur_validity = make_validity(CUR_N, rng=rng)
    shifted = condition in ('drift_only', 'both')
    degraded = condition in ('invalid_only', 'both')
    if degraded:
        cur_validity = shift_validity(cur_validity, DEGRADED_P_VALID, rng=rng)
    n_success = int(cur_validity.sum())

    if n_success >= 2:
        cur_lengths = make_lengths(n_success, rng=rng)
        cur_embeddings = make_embeddings(n_success, rng=rng)
        if shifted:
            cur_lengths = shift_lengths(
                cur_lengths, delta_mean=LENGTH_DELTA * DEFAULT_LENGTH_STD)
            cur_embeddings = shift_embeddings(cur_embeddings, delta, rng=rng)
        _, ks_p = ks_test(ref_lengths, cur_lengths)
        _, mmd_p = mmd_test(ref_embeddings.tolist(), cur_embeddings.tolist(),
                            n_permutations=MMD_PERMUTATIONS, random_seed=trial_seed)
    else:
        ks_p, mmd_p = 1.0, 1.0

    _, val_p, _ = proportion_test(list(ref_validity), list(cur_validity))

    _, dist_flags = benjamini_hochberg_correction([ks_p, mmd_p], alpha=0.05)
    _, all_flags = benjamini_hochberg_correction([ks_p, val_p, mmd_p], alpha=0.05)

    return {
        'condition': condition,
        'degraded': int(degraded),
        'shifted': int(shifted),
        'n_success': n_success,
        'ks': int(ks_p < 0.05),
        'mmd': int(mmd_p < 0.05),
        'validity': int(val_p < 0.05),
        'dist_bh': int(any(dist_flags)),
        'pooled_bh': int(any(all_flags)),
    }


def proxy_metrics(rows, signal: str) -> dict:
    """Conditional probabilities of a flag as a regression proxy, with Laplace
    smoothing so empty cells read as 0 rather than dividing by zero. These are
    conditional on the simulated condition mixture and are not universal
    production precision estimates."""
    n = len(rows)
    flagged = [r for r in rows if r[signal] == 1]
    degraded = [r for r in rows if r['degraded'] == 1]
    quiet = [r for r in rows if r['degraded'] == 0]
    p_flag_given_deg = (sum(r[signal] for r in degraded) + 1.0) / (len(degraded) + 2.0)
    p_deg_given_flag = (sum(1 for r in flagged if r['degraded'] == 1) + 1.0) / (len(flagged) + 2.0)
    benign_flag_rate = (sum(r[signal] for r in quiet) + 1.0) / (len(quiet) + 2.0)
    return {
        'signal': signal,
        'n_trials': n,
        'p_regression_given_flag': round(p_deg_given_flag, 4),
        'p_flag_given_regression': round(p_flag_given_deg, 4),
        'benign_flag_rate': round(benign_flag_rate, 4),
    }


def draw_condition_with_prior(rng, pi_benign: float) -> str:
    """Mixture with a controllable base rate of benign distribution shifts.

    P(drift_only) = pi_benign, invalid_only and both fixed at 0.2 each, null
    takes the rest. Sweeping pi_benign is the sensitivity analysis that
    matters for the proxy question: the value of a drift flag depends on how
    often the world produces harmless distribution change.
    """
    u = rng.random()
    if u < pi_benign:
        return 'drift_only'
    if u < pi_benign + 0.2:
        return 'both'
    if u < pi_benign + 0.4:
        return 'invalid_only'
    return 'null'


def run(
    seed: int = 42,
    n_trials: int = 100,
    delta: float = DEFAULT_DELTA,
    curve: bool = True,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    print(f"[reliability] 2x2 drift x invalid-response regression study, "
          f"exactly {n_trials} trials per condition, delta={delta}, "
          f"production parity ({REF_N} ref, {CUR_N} cur, "
          f"{MMD_PERMUTATIONS} permutations)...")

    rng = np.random.default_rng(seed)
    ref_validity = make_validity(REF_N, rng=rng)
    n_ref_success = int(ref_validity.sum())
    ref_lengths = make_lengths(n_ref_success, rng=rng)
    ref_embeddings = make_embeddings(n_ref_success, rng=rng)

    # Stratified: exactly n_trials per condition, so every reported cell has
    # the same sample size and the tables need no per-condition caveat.
    rows = []
    for c_idx, condition in enumerate(CONDITIONS):
        for i in range(n_trials):
            trial_seed = seed * 10000 + c_idx * 1000 + i * 37
            rows.append(run_trial(condition, trial_seed, delta,
                                  ref_lengths, ref_validity, ref_embeddings))

    # Detection rate per condition per signal.
    condition_rows = []
    for condition in CONDITIONS:
        subset = [r for r in rows if r['condition'] == condition]
        for signal in SIGNALS:
            rate = sum(r[signal] for r in subset) / len(subset)
            condition_rows.append({
                'condition': condition, 'signal': signal,
                'n_trials': len(subset), 'detection_rate': round(rate, 4),
            })
            print(f"[reliability] {condition:>12} | {signal:>9} | {rate:.2f}")

    # Proxy quality of each flag on the equal mixture.
    proxy_rows = []
    print("[reliability] proxy metrics on the equal mixture (Laplace smoothed, "
          "conditional on this simulated mixture):")
    for signal in SIGNALS:
        m = proxy_metrics(rows, signal)
        proxy_rows.append(m)
        print(f"[reliability] {signal:>9} | P(reg|flag)={m['p_regression_given_flag']:.2f} "
              f"| P(flag|reg)={m['p_flag_given_regression']:.2f} "
              f"| benign flag rate={m['benign_flag_rate']:.2f}")

    # Base-rate sensitivity curve: proxy precision as the prevalence of
    # benign distribution shifts grows. This is the "when" of the research
    # question.
    curve_rows = []
    if curve:
        pi_grid = [0.0, 0.1, 0.2, 0.3, 0.4]
        print("[reliability] base-rate sensitivity curve:")
        for pi in pi_grid:
            curve_rows_seed = []
            for i in range(n_trials):
                trial_seed = seed * 10000 + 5000 + i * 41
                condition = draw_condition_with_prior(
                    np.random.default_rng(trial_seed), pi)
                curve_rows_seed.append(run_trial(
                    condition, trial_seed, delta,
                    ref_lengths, ref_validity, ref_embeddings))
            dist_m = proxy_metrics(curve_rows_seed, 'dist_bh')
            pooled_m = proxy_metrics(curve_rows_seed, 'pooled_bh')
            valid_m = proxy_metrics(curve_rows_seed, 'validity')
            curve_rows.append({
                'pi_benign': pi,
                'dist_precision': dist_m['p_regression_given_flag'],
                'pooled_precision': pooled_m['p_regression_given_flag'],
                'validity_precision': valid_m['p_regression_given_flag'],
            })
            print(f"[reliability] pi_benign={pi:.1f} | dist P(reg|flag)="
                  f"{dist_m['p_regression_given_flag']:.2f} | pooled P(reg|flag)="
                  f"{pooled_m['p_regression_given_flag']:.2f} | validity "
                  f"P(reg|flag)={valid_m['p_regression_given_flag']:.2f}")

    # Persist everything.
    csv_path = os.path.join(out_dir, 'reliability_results.csv')
    fieldnames = ['row_type', 'condition', 'signal', 'n_trials',
                  'detection_rate', 'p_regression_given_flag',
                  'p_flag_given_regression', 'benign_flag_rate', 'pi_benign',
                  'dist_precision', 'pooled_precision', 'validity_precision']
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, restval='')
        w.writeheader()
        for r in condition_rows:
            w.writerow({'row_type': 'condition', **r})
        for m in proxy_rows:
            w.writerow({'row_type': 'proxy', **m})
        for c in curve_rows:
            w.writerow({'row_type': 'curve', **c})

    # Plot: detection rates and the base-rate sensitivity curve.
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    width = 0.16
    xs = np.arange(len(CONDITIONS))
    colors = {'ks': '#94a3b8', 'mmd': '#3b82f6', 'validity': '#f59e0b',
              'dist_bh': '#8b5cf6', 'pooled_bh': '#ef4444'}
    for j, signal in enumerate(SIGNALS):
        rates = [next(r['detection_rate'] for r in condition_rows
                      if r['condition'] == c and r['signal'] == signal)
                 for c in CONDITIONS]
        ax1.bar(xs + (j - 2) * width, rates, width, label=signal,
                color=colors[signal], alpha=0.9)
    ax1.set_xticks(xs)
    ax1.set_xticklabels(CONDITIONS)
    ax1.set_ylim(0, 1.05)
    ax1.set_title('Detection rate by condition and signal', fontsize=11, fontweight='bold')
    ax1.set_ylabel('Detection rate')
    ax1.grid(True, alpha=0.3, axis='y')
    ax1.legend(fontsize=8)

    if curve_rows:
        xs2 = [c['pi_benign'] for c in curve_rows]
        ax2.plot(xs2, [c['dist_precision'] for c in curve_rows], 'o-',
                 color='#8b5cf6', label='Distributional family only')
        ax2.plot(xs2, [c['pooled_precision'] for c in curve_rows], 's-',
                 color='#ef4444', label='Deployed pooled pipeline')
        ax2.plot(xs2, [c['validity_precision'] for c in curve_rows], '^:',
                 color='#f59e0b', label='Behavioral validity signal only')
        ax2.set_xlabel('Base rate of benign distribution shifts')
        ax2.set_ylabel('P(invalid-response regression | flag)')
        ax2.set_ylim(0, 1.05)
        ax2.set_title('Drift flag as a regression proxy, by benign shift base rate',
                      fontsize=11, fontweight='bold')
        ax2.grid(True, alpha=0.3)
        ax2.legend(fontsize=9)
    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'reliability.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[reliability] Complete. Saved to {csv_path} and {plot_path}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_reliability_seed{seed}",
                   experiment_name="llm-drift-experiments",
                   tags={"experiment": "reliability"}) as run_obj:
        log_drift_run(
            run_obj,
            params={
                'experiment': 'reliability', 'seed': seed,
                'n_trials': n_trials, 'delta': delta,
                'ref_n': REF_N, 'cur_n': CUR_N,
                'mmd_permutations': MMD_PERMUTATIONS,
                'degraded_p_valid': DEGRADED_P_VALID,
                'git_hash': get_git_hash(),
            },
            metrics={
                **{f"detection_{c['condition']}_{c['signal']}": c['detection_rate']
                   for c in condition_rows},
                **{f"proxy_{m['signal']}": m['p_regression_given_flag']
                   for m in proxy_rows},
                **{f"curve_dist_precision_{c['pi_benign']}": c['dist_precision']
                   for c in curve_rows},
                **{f"curve_pooled_precision_{c['pi_benign']}": c['pooled_precision']
                   for c in curve_rows},
            },
            artifact_files=[csv_path, plot_path]
        )

    return {'condition_rows': condition_rows, 'proxy_rows': proxy_rows,
            'curve_rows': curve_rows}


def main():
    parser = argparse.ArgumentParser(
        description='Drift as a proxy for invalid-response regression: the 2x2 study')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-trials', type=int, default=100)
    parser.add_argument('--delta', type=float, default=DEFAULT_DELTA)
    parser.add_argument('--no-curve', action='store_true')
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(seed=args.seed, n_trials=args.n_trials, delta=args.delta,
        curve=not args.no_curve, out_dir=args.out_dir)


if __name__ == '__main__':
    main()
