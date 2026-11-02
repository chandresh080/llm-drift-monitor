"""Detection delay and false-alarm simulation over a 90-day window horizon."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test
from experiments.sim import make_lengths, DEFAULT_LENGTH_STD


DAYS = 90
SHIFT_DAY = 45
N_PER_WINDOW = 10
K_OF_N_M = 5  # window length for k-of-n
K_OF_N_K = 2  # reject if >= k of last m reject
CUSUM_H = 2.0  # CUSUM threshold on drift score


def run(seed: int = 42, out_dir=None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    day_rows = []
    alerts = {'single': [], 'kofn': [], 'cusum': []}
    first_alert = {'single': None, 'kofn': None, 'cusum': None}
    cusum_stat = 0.0
    rejects = []

    for day in range(DAYS):
        rng = np.random.default_rng(seed + day)
        ref_len = make_lengths(N_PER_WINDOW, rng=rng)
        cur_len = make_lengths(N_PER_WINDOW, rng=rng)
        stat, p = ks_test(ref_len, cur_len)
        reject = bool(p < 0.05)
        rejects.append(reject)

        # drift score: standardized mean length difference
        drift_score = (float(np.mean(cur_len)) - float(np.mean(ref_len))) / DEFAULT_LENGTH_STD
        cusum_stat = max(cusum_stat + drift_score, 0.0) - max(cusum_stat - 0.0, 0.0) if False else cusum_stat + drift_score
        cusum_alert = cusum_stat > CUSUM_H
        if cusum_alert:
            cusum_stat = 0.0
        if cusum_alert and day >= SHIFT_DAY:
            alerts['cusum'].append(day)
            if first_alert['cusum'] is None:
                first_alert['cusum'] = day

        # single-window alert
        if day >= SHIFT_DAY and reject:
            alerts['single'].append(day)
            if first_alert['single'] is None:
                first_alert['single'] = day

        # k-of-n: alert if >= k of last m windows reject
        window_flags = rejects[max(0, day - K_OF_N_M + 1):day + 1]
        kofn_reject = sum(window_flags) >= K_OF_N_K
        if kofn_reject and day >= SHIFT_DAY:
            alerts['kofn'].append(day)
            if first_alert['kofn'] is None:
                first_alert['kofn'] = day

        day_rows.append({
            'day': day,
            'ks_p': float(p),
            'reject': int(reject),
            'drift_score': float(drift_score),
            'cusum': float(cusum_stat),
            'cusum_alert': int(cusum_alert),
        })

    # False alarms per 100 days during baseline (days < SHIFT_DAY)
    baseline_days = SHIFT_DAY  # days 0..44
    false_alarms = {
        'single': len([d for d in alerts['single'] if d < SHIFT_DAY]),
        'kofn': len([d for d in alerts['kofn'] if d < SHIFT_DAY]),
        'cusum': len([d for d in alerts['cusum'] if d < SHIFT_DAY]),
    }
    fa_rows = []
    for rule, n_alerts in false_alarms.items():
        fa_rows.append({
            'rule': rule,
            'alpha': 0.05,
            'horizon_days': DAYS,
            'shift_day': SHIFT_DAY,
            'baseline_days': baseline_days,
            'false_alarms': n_alerts,
            'false_alarms_per_100_days': n_alerts / baseline_days * 100.0 if baseline_days > 0 else 0.0,
        })

    delay_rows = []
    for rule, first in first_alert.items():
        delay = (first - SHIFT_DAY) if first is not None else (DAYS - SHIFT_DAY)
        delay_rows.append({
            'rule': rule,
            'alpha': 0.05,
            'shift_day': SHIFT_DAY,
            'first_alert_day': first,
            'mean_detection_delay': delay,
        })

    alerts_csv = os.path.join(out_dir, 'delay_alerts.csv')
    with open(alerts_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(day_rows[0].keys()))
        w.writeheader()
        w.writerows(day_rows)

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

    fig, ax = plt.subplots(figsize=(8, 5))
    for rule, n_alerts in false_alarms.items():
        ax.bar(rule, n_alerts / baseline_days * 100.0, label=f'{rule} false alarms / 100 days')
    ax.set_ylabel('False alarms (per 100 baseline days)')
    ax.set_title('False alarms over baseline window')
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, 'delay_false_alarm_rate.png'))
    plt.close()

    return {
        'false_alarms': fa_rows,
        'detection_delay': delay_rows,
    }


def main():
    parser = argparse.ArgumentParser(description='Detection delay / false alarm simulation')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    out = run(seed=args.seed)
    print('Delay analysis complete:', out)


if __name__ == '__main__':
    main()
