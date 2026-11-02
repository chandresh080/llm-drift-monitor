"""Power analysis: detect injected shifts of known size."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, two_proportion_z_test, mmd_test
from experiments.sim import make_lengths, make_validity, make_embeddings, shift_embeddings, EMBEDDING_STD


DELTAS = [0.0, 0.5, 1.0, 2.0, 3.0, 4.0]
NS = [10, 25, 50, 100]
N_SIMS = 20


def run(seed: int = 42, out_dir=None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for test in ['ks', 'z', 'mmd']:
        for delta in DELTAS:
            for n in NS:
                rej = 0
                for i in range(N_SIMS):
                    rng = np.random.default_rng(seed + i)
                    if test == 'ks':
                        ref = make_lengths(n, rng=rng)
                        cur = make_lengths(n, mean=ref.mean() + delta * 15.0, rng=rng)
                        stat, p = ks_test(ref, cur)
                    elif test == 'z':
                        ref = make_validity(n, 0.95, rng=rng)
                        cur = make_validity(n, max(0.05, 0.95 - delta), rng=rng)
                        stat, p = two_proportion_z_test(ref, cur)
                    else:
                        ref = make_embeddings(n, rng=rng)
                        cur = shift_embeddings(ref, delta, std=EMBEDDING_STD)
                        stat, p = mmd_test(ref, cur, n_permutations=100, random_seed=42)
                    if p < 0.05:
                        rej += 1
                results.append({
                    'test': test, 'delta': delta, 'n': n,
                    'power': rej / N_SIMS, 'n_sims': N_SIMS,
                })

    csv_path = os.path.join(out_dir, 'power_curves.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    fig, ax = plt.subplots(figsize=(8, 5))
    for n in NS:
        rows = [r for r in results if r['test'] == 'ks' and r['n'] == n]
        if rows:
            ax.plot([r['delta'] for r in rows], [r['power'] for r in rows], marker='o', label=f'KS (n={n})')
    for n in NS:
        rows = [r for r in results if r['test'] == 'z' and r['n'] == n]
        if rows:
            ax.plot([r['delta'] for r in rows], [r['power'] for r in rows], marker='s', label=f'Z/refusal (n={n})')
    for n in NS:
        rows = [r for r in results if r['test'] == 'mmd' and r['n'] == n]
        if rows:
            ax.plot([r['delta'] for r in rows], [r['power'] for r in rows], marker='^', label=f'MMD (n={n})')
    ax.set_xlabel('Shift size (delta, std units / refusal units)')
    ax.set_ylabel('Power (rejection rate @ alpha=0.05)')
    ax.set_title('Drift detector power vs shift size')
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, 'power_curves.png'))
    plt.close()

    return {'results': results}


def main():
    parser = argparse.ArgumentParser(description='Power analysis')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    out = run(seed=args.seed)
    print('Power analysis complete:', len(out['results']), 'rows')


if __name__ == '__main__':
    main()
