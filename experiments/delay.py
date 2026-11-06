"""Detection delay and false-alarm simulation over a 90-day window horizon.

The per-day decision reproduces the deployed monitoring pipeline: three
detectors (KS on lengths, the proportion test on validity, MMD on
embeddings), Benjamini Hochberg correction across the three, then the alert
rules applied to the pooled flagged stream. The deployed policy is the
persistence rule: an alert fires only after two consecutive flagged cycles.
The category-wise tests and the optional alpha-spending gate are not part of
this simulation and are called out in the README.
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

DAYS = 90
SHIFT_DAY = 45
N_PER_WINDOW = 25
K_OF_N_M = 5  # sliding window size
K_OF_N_K = 2  # threshold rejections in window
PERSIST_K = 2  # deployed policy: consecutive flagged cycles before alerting
CUSUM_K = 0.5  # CUSUM reference allowance (half shift size)
CUSUM_H = 3.5  # CUSUM decision boundary threshold
SHIFT_MAGNITUDE = 1.2  # shift in std units
SHIFT_P_VALID = 0.70  # post-shift valid fraction (baseline 0.95)
MMD_PERMUTATIONS = 300  # permutation resolution for the daily MMD test


def run(
    seed: int = 42,
    n_trials: int = 100,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    print(f"[delay] Running 90-day sequential horizon across {n_trials} Monte Carlo trials (shift at day {SHIFT_DAY})...")

    trial_delays = {'single': [], 'kofn': [], 'persist2': [], 'cusum': []}
    trial_false_alarms = {'single': [], 'kofn': [], 'persist2': [], 'cusum': []}

    sample_daily_trajectory = []

    for trial in range(n_trials):
        trial_seed = seed * 10000 + trial * 97
        rng = np.random.default_rng(trial_seed)

        # Baseline reference windows fixed per monitoring deployment
        ref_samples = make_lengths(N_PER_WINDOW * 2, rng=rng)
        ref_validity = make_validity(N_PER_WINDOW * 2, rng=rng)
        ref_embeddings = make_embeddings(N_PER_WINDOW * 2, rng=rng)

        single_rejects = []
        cusum_stat = 0.0

        fa_counts = {'single': 0, 'kofn': 0, 'persist2': 0, 'cusum': 0}
        first_alert = {'single': None, 'kofn': None, 'persist2': None, 'cusum': None}

        for day in range(DAYS):
            day_rng = np.random.default_rng(trial_seed + day)
            cur_samples = make_lengths(N_PER_WINDOW, rng=day_rng)

            # Inject shift on or after SHIFT_DAY: the same behavioral
            # change hits all three detectors, as a real model regression would.
            is_shifted = (day >= SHIFT_DAY)
            if is_shifted:
                cur_samples = shift_lengths(cur_samples, delta_mean=SHIFT_MAGNITUDE * DEFAULT_LENGTH_STD)
            cur_validity = make_validity(N_PER_WINDOW, rng=day_rng)
            cur_embeddings = make_embeddings(N_PER_WINDOW, rng=day_rng)
            if is_shifted:
                cur_validity = shift_validity(cur_validity, SHIFT_P_VALID, rng=day_rng)
                cur_embeddings = shift_embeddings(cur_embeddings, SHIFT_MAGNITUDE, rng=day_rng)

            # The deployed pooled decision: three detectors, BH across them.
            _, ks_p = ks_test(ref_samples, cur_samples)
            _, prop_p, _ = proportion_test(list(ref_validity), list(cur_validity))
            _, mmd_p = mmd_test(ref_embeddings.tolist(), cur_embeddings.tolist(),
                                n_permutations=MMD_PERMUTATIONS,
                                random_seed=trial_seed + day)
            _, bh_flags = benjamini_hochberg_correction([ks_p, prop_p, mmd_p], alpha=0.05)
            pooled_flag = any(bh_flags)
            single_alert = bool(pooled_flag)
            single_rejects.append(single_alert)

            # 2. k-of-n persistence
            window = single_rejects[max(0, day - K_OF_N_M + 1):day + 1]
            kofn_alert = bool(sum(window) >= K_OF_N_K)

            # 2b. Deployed alert policy: the per cycle rejection must persist
            # for PERSIST_K consecutive cycles. This is the rule the monitor
            # actually gates alerts on (alerts.min_consecutive_cycles).
            streak_window = single_rejects[max(0, day - PERSIST_K + 1):day + 1]
            persist2_alert = bool(len(streak_window) == PERSIST_K
                                  and all(streak_window))

            # 3. CUSUM on standardized drift score
            # Drift score = normalized sample mean difference
            drift_score = (float(np.mean(cur_samples)) - float(np.mean(ref_samples))) / (DEFAULT_LENGTH_STD / np.sqrt(N_PER_WINDOW))
            cusum_stat = max(0.0, cusum_stat + drift_score - CUSUM_K)
            cusum_alert = bool(cusum_stat > CUSUM_H)
            if cusum_alert:
                cusum_stat = 0.0  # reset after alarm

            # Record alerts
            alerts_today = {
                'single': single_alert,
                'kofn': kofn_alert,
                'persist2': persist2_alert,
                'cusum': cusum_alert
            }

            for rule, alerted in alerts_today.items():
                if alerted:
                    if not is_shifted:
                        fa_counts[rule] += 1
                    else:
                        if first_alert[rule] is None:
                            first_alert[rule] = day

            # Save first trial trajectory for reporting and plotting
            if trial == 0:
                sample_daily_trajectory.append({
                    'day': day,
                    'is_shifted': int(is_shifted),
                    'ks_p': float(ks_p),
                    'prop_p': float(prop_p),
                    'mmd_p': float(mmd_p),
                    'pooled_flag': int(pooled_flag),
                    'single_alert': int(single_alert),
                    'kofn_alert': int(kofn_alert),
                    'persist2_alert': int(persist2_alert),
                    'drift_score': float(drift_score),
                    'cusum_stat': float(cusum_stat),
                    'cusum_alert': int(cusum_alert),
                })

        for rule in ['single', 'kofn', 'persist2', 'cusum']:
            trial_false_alarms[rule].append(fa_counts[rule])
            # If never alerted after day 45, censor at maximum horizon delay
            delay = (first_alert[rule] - SHIFT_DAY) if first_alert[rule] is not None else (DAYS - SHIFT_DAY)
            trial_delays[rule].append(delay)

    # Summary tables
    baseline_days = SHIFT_DAY
    fa_rows = []
    delay_rows = []

    for rule in ['single', 'kofn', 'persist2', 'cusum']:
        mean_fa = float(np.mean(trial_false_alarms[rule]))
        fa_per_100_days = float((mean_fa / baseline_days) * 100.0)
        mean_delay = float(np.mean(trial_delays[rule]))
        median_delay = float(np.median(trial_delays[rule]))
        detection_rate = float(np.mean([d < (DAYS - SHIFT_DAY) for d in trial_delays[rule]]))

        fa_rows.append({
            'rule': rule,
            'alpha': 0.05,
            'horizon_days': DAYS,
            'shift_day': SHIFT_DAY,
            'baseline_days': baseline_days,
            'mean_false_alarms_in_baseline': mean_fa,
            'false_alarms_per_100_days': round(fa_per_100_days, 2),
        })

        delay_rows.append({
            'rule': rule,
            'alpha': 0.05,
            'shift_day': SHIFT_DAY,
            'shift_magnitude_sigma': SHIFT_MAGNITUDE,
            'mean_detection_delay_days': round(mean_delay, 2),
            'median_detection_delay_days': round(median_delay, 2),
            'detection_rate_pct': round(detection_rate * 100.0, 1),
        })

    # Save CSVs
    fa_csv = os.path.join(out_dir, 'delay_false_alarms.csv')
    with open(fa_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(fa_rows[0].keys()))
        w.writeheader()
        w.writerows(fa_rows)

    delay_csv = os.path.join(out_dir, 'delay_detection_delay.csv')
    with open(delay_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(delay_rows[0].keys()))
        w.writeheader()
        w.writerows(delay_rows)

    traj_csv = os.path.join(out_dir, 'delay_alerts.csv')
    with open(traj_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(sample_daily_trajectory[0].keys()))
        w.writeheader()
        w.writerows(sample_daily_trajectory)

    # Plot Tradeoff: False Alarm Rate vs Mean Detection Delay
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))

    # Scatter plot: Delay vs False Alarm Rate
    rule_labels = {'single': 'Single Window (p < 0.05)', 'kofn': f'k-of-n ({K_OF_N_K}-of-{K_OF_N_M})', 'persist2': f'Deployed policy ({PERSIST_K} consecutive)', 'cusum': f'CUSUM (h={CUSUM_H})'}
    colors = {'single': '#ef4444', 'kofn': '#3b82f6', 'persist2': '#f59e0b', 'cusum': '#10b981'}
    markers = {'single': 'o', 'kofn': 's', 'persist2': 'D', 'cusum': '^'}

    for r_item, d_item in zip(fa_rows, delay_rows):
        rule = r_item['rule']
        x = r_item['false_alarms_per_100_days']
        y = d_item['mean_detection_delay_days']
        ax1.scatter(x, y, color=colors[rule], marker=markers[rule], s=140, label=rule_labels[rule], zorder=5)
        ax1.annotate(f" {rule.upper()}\n (FA: {x:.1f}, Delay: {y:.1f}d)", (x, y), textcoords="offset points", xytext=(8, -4), fontsize=9)

    ax1.set_title('Operational Trade-off: Detection Delay vs False Alarms', fontsize=11, fontweight='bold')
    ax1.set_xlabel('False Alarms per 100 Days (Lower is Better)', fontsize=10)
    ax1.set_ylabel('Mean Detection Delay in Days (Lower is Better)', fontsize=10)
    ax1.grid(True, alpha=0.3)
    ax1.legend(loc='upper right', fontsize=9)

    # Subplot 2: Trajectory of trial 0 showing CUSUM vs Single alert behavior
    days = [r['day'] for r in sample_daily_trajectory]
    cusum_vals = [r['cusum_stat'] for r in sample_daily_trajectory]
    ax2.plot(days, cusum_vals, color='#10b981', linewidth=1.8, label='CUSUM Statistic')
    ax2.axhline(CUSUM_H, color='red', linestyle='--', label=f'Threshold h={CUSUM_H}')
    ax2.axvline(SHIFT_DAY, color='gray', linestyle=':', linewidth=1.5, label=f'Injected Shift (Day {SHIFT_DAY})')
    ax2.set_title('Sample 90-Day Deployment Run: CUSUM Drift Tracking', fontsize=11, fontweight='bold')
    ax2.set_xlabel('Day', fontsize=10)
    ax2.set_ylabel('CUSUM Statistic', fontsize=10)
    ax2.grid(True, alpha=0.3)
    ax2.legend(loc='upper left', fontsize=9)

    plt.suptitle('Sequential Monitoring Benchmark (90-Day Simulation with Day 45 Drift)', fontsize=13, y=1.02)
    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'delay_false_alarm_rate.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[delay] Complete. Saved to {out_dir}")
    for r, d in zip(fa_rows, delay_rows):
        print(f"  {r['rule']:<8} | False Alarms/100d: {r['false_alarms_per_100_days']:<5.1f} | Mean Delay: {d['mean_detection_delay_days']} days | Detect Rate: {d['detection_rate_pct']}%")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_delay_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "delay"}) as run:
        delay_metrics = {}
        for r_item in fa_rows:
            delay_metrics[f"false_alarms_per_100d_{r_item['rule']}"] = r_item['false_alarms_per_100_days']
        for d_item in delay_rows:
            delay_metrics[f"mean_delay_{d_item['rule']}"] = d_item['mean_detection_delay_days']
            delay_metrics[f"detect_rate_{d_item['rule']}"] = d_item['detection_rate_pct']

        log_drift_run(
            run,
            params={
                'experiment': 'delay',
                'seed': seed,
                'n_trials': n_trials,
                'shift_day': SHIFT_DAY,
                'horizon_days': DAYS,
                'shift_magnitude_sigma': SHIFT_MAGNITUDE,
                'git_hash': get_git_hash(),
            },
            metrics=delay_metrics,
            artifact_files=[plot_path, fa_csv, delay_csv, traj_csv]
        )

    return {'false_alarms': fa_rows, 'delay': delay_rows}


def main():
    parser = argparse.ArgumentParser(description='Sequential detection delay vs false alarm benchmark')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-trials', type=int, default=100)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(seed=args.seed, n_trials=args.n_trials, out_dir=args.out_dir)


if __name__ == '__main__':
    main()
