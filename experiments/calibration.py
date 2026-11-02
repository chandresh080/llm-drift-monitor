"""Calibration: false-positive rates and p-value histograms for drift tests."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, two_proportion_z_test, mmd_test, benjamini_hochberg_correction
from experiments.sim import make_lengths, make_validity, make_embeddings, DEFAULT_VALID_P


def run(n_sims: int = 1000, seed: int = 42, alpha: float = 0.05, n_per_window: int = 20, n_permutations: int = 25, out_dir=None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for i in range(n_sims):
        rng = np.random.default_rng(seed + i)
        ref_len = make_lengths(n_per_window, rng=rng)
        cur_len = make_lengths(n_per_window, rng=rng)
        ref_val = make_validity(n_per_window, DEFAULT_VALID_P, rng=rng)
        cur_val = make_validity(n_per_window, DEFAULT_VALID_P, rng=rng)
        ref_emb = make_embeddings(n_per_window, rng=rng)
        cur_emb = make_embeddings(n_per_window, rng=rng)

        ks_stat, ks_p = ks_test(ref_len, cur_len)
        z_stat, z_p = two_proportion_z_test(ref_val, cur_val)
        mmd_stat, mmd_p = mmd_test(ref_emb, cur_emb, n_permutations=n_permutations, random_seed=42)
        adj, sig = benjamini_hochberg_correction([ks_p, z_p, mmd_p], alpha=alpha)

        rows.append({
            'sim': i,
            'ks_stat': float(ks_stat), 'ks_p': float(ks_p),
            'z_stat': float(z_stat), 'z_p': float(z_p),
            'mmd_stat': float(mmd_stat), 'mmd_p': float(mmd_p),
            'bh_ks': float(adj[0]), 'bh_z': float(adj[1]), 'bh_mmd': float(adj[2]),
            'bh_any': int(any(sig)),
        })

    csv_path = os.path.join(out_dir, 'calibration_pvalues.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    fpr_rows = []
    for name, pkey, bkey in [('ks', 'ks_p', 'bh_ks'), ('z', 'z_p', 'bh_z'), ('mmd', 'mmd_p', 'bh_mmd')]:
        raw_p = np.array([r[pkey] for r in rows])
        bh_p = np.array([r[bkey] for r in rows])
        fpr_rows.append({
            'test': name,
            'alpha': alpha,
            'n_sims': n_sims,
            'fpr_raw': float(np.mean(raw_p < alpha)),
            'fpr_bh': float(np.mean(bh_p < alpha)),
        })
    fpr_csv = os.path.join(out_dir, 'calibration_fpr.csv')
    with open(fpr_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(fpr_rows[0].keys()))
        w.writeheader()
        w.writerows(fpr_rows)

    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    for ax, name, pkey in zip(axes, ['ks', 'z', 'mmd'], ['ks_p', 'z_p', 'mmd_p']):
        ax.hist([r[pkey] for r in rows], bins=20, range=(0, 1), density=True)
        ax.set_title(f'P-value histogram: {name}')
        ax.set_xlabel('p-value')
        ax.set_ylabel('density')
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, 'calibration_pvalue_histogram.png'))
    plt.close()

    return {'n_sims': n_sims, 'fpr': fpr_rows}


def main():
    parser = argparse.ArgumentParser(description='Calibration of drift tests')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-sims', type=int, default=200)
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--n-per-window', type=int, default=20)
    parser.add_argument('--n-permutations', type=int, default=10)
    args = parser.parse_args()
    out = run(n_sims=args.n_sims, seed=args.seed, alpha=args.alpha, n_per_window=args.n_per_window, n_permutations=args.n_permutations)
    print('Calibration complete:', out)


if __name__ == '__main__':
    main()
