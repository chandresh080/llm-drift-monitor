"""Calibration: false-positive rates and p-value uniformity under null distribution."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, two_proportion_z_test, mmd_test, benjamini_hochberg_correction
from experiments.sim import make_lengths, make_validity, make_embeddings, DEFAULT_VALID_P


def run(
    n_sims: int = 1000,
    seed: int = 42,
    alpha: float = 0.05,
    n_per_window: int = 50,
    n_permutations: int = 100,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    print(f"[calibration] Running {n_sims} null simulations (N={n_per_window}, alpha={alpha}, seed={seed})...")

    for i in range(n_sims):
        rng_ref = np.random.default_rng(seed * 10000 + i * 2)
        rng_cur = np.random.default_rng(seed * 10000 + i * 2 + 1)

        ref_len = make_lengths(n_per_window, rng=rng_ref)
        cur_len = make_lengths(n_per_window, rng=rng_cur)

        ref_val = make_validity(n_per_window, p_valid=DEFAULT_VALID_P, rng=rng_ref)
        cur_val = make_validity(n_per_window, p_valid=DEFAULT_VALID_P, rng=rng_cur)

        ref_emb = make_embeddings(n_per_window, rng=rng_ref)
        cur_emb = make_embeddings(n_per_window, rng=rng_cur)

        ks_stat, ks_p = ks_test(ref_len, cur_len)
        z_stat, z_p = two_proportion_z_test(ref_val, cur_val)
        mmd_stat, mmd_p = mmd_test(ref_emb, cur_emb, n_permutations=n_permutations, random_seed=seed + i)

        # Multiple testing correction
        adj, sig = benjamini_hochberg_correction([ks_p, z_p, mmd_p], alpha=alpha)

        rows.append({
            'sim': i,
            'ks_stat': float(ks_stat),
            'ks_p': float(ks_p),
            'z_stat': float(z_stat),
            'z_p': float(z_p),
            'mmd_stat': float(mmd_stat),
            'mmd_p': float(mmd_p),
            'bh_ks': float(adj[0]),
            'bh_z': float(adj[1]),
            'bh_mmd': float(adj[2]),
            'sig_ks': int(ks_p < alpha),
            'sig_z': int(z_p < alpha),
            'sig_mmd': int(mmd_p < alpha),
            'bh_sig_ks': int(sig[0]),
            'bh_sig_z': int(sig[1]),
            'bh_sig_mmd': int(sig[2]),
            'bh_any': int(any(sig)),
        })

    # Save detailed per-simulation p-values
    csv_path = os.path.join(out_dir, 'calibration_pvalues.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # Compute False Positive Rates
    fpr_rows = []
    for name, pkey, bkey in [('ks', 'ks_p', 'bh_ks'), ('z', 'z_p', 'bh_z'), ('mmd', 'mmd_p', 'bh_mmd')]:
        raw_p = np.array([r[pkey] for r in rows if not np.isnan(r[pkey])])
        bh_p = np.array([r[bkey] for r in rows if not np.isnan(r[bkey])])
        fpr_rows.append({
            'test': name,
            'alpha': alpha,
            'n_sims': len(raw_p),
            'fpr_raw': float(np.mean(raw_p < alpha)),
            'fpr_bh': float(np.mean(bh_p < alpha)),
            'mean_p': float(np.mean(raw_p)),
        })

    # Family-wise / False discovery across any test
    any_raw = [int(r['sig_ks'] or r['sig_z'] or r['sig_mmd']) for r in rows]
    any_bh = [r['bh_any'] for r in rows]
    fpr_rows.append({
        'test': 'any_detector',
        'alpha': alpha,
        'n_sims': n_sims,
        'fpr_raw': float(np.mean(any_raw)),
        'fpr_bh': float(np.mean(any_bh)),
        'mean_p': float('nan'),
    })

    fpr_csv = os.path.join(out_dir, 'calibration_fpr.csv')
    with open(fpr_csv, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(fpr_rows[0].keys()))
        w.writeheader()
        w.writerows(fpr_rows)

    # Plot 3-panel p-value histogram
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    test_titles = {
        'ks': ('Kolmogorov-Smirnov (Lengths)', 'ks_p'),
        'z': ('Two-Proportion Z-Test (Refusal/Validity)', 'z_p'),
        'mmd': ('MMD with Median Heuristic (Embeddings)', 'mmd_p')
    }

    for ax, (test_name, (title, pkey)) in zip(axes, test_titles.items()):
        vals = [r[pkey] for r in rows if not np.isnan(r[pkey])]
        ax.hist(vals, bins=20, range=(0.0, 1.0), density=True, color='#2563eb', alpha=0.7, edgecolor='white')
        ax.axhline(1.0, color='red', linestyle='--', linewidth=1.5, label='Ideal Uniform Null')
        ax.axvline(alpha, color='black', linestyle=':', linewidth=1.2, label=f'alpha={alpha}')
        ax.set_title(title, fontsize=11, fontweight='bold')
        ax.set_xlabel('p-value', fontsize=10)
        ax.set_ylabel('Empirical Density', fontsize=10)
        ax.set_xlim(0.0, 1.0)
        ax.legend(loc='upper right', fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.suptitle('Null Distribution Calibration: P-Value Histograms under H0 (1000 Simulations)', fontsize=13, y=1.02)
    plt.tight_layout()
    hist_path = os.path.join(out_dir, 'calibration_pvalue_histogram.png')
    plt.savefig(hist_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[calibration] Done. Results written to {out_dir}")
    for row in fpr_rows:
        print(f"  {row['test']:<14} | Raw FPR: {row['fpr_raw']:.4f} | BH FPR: {row['fpr_bh']:.4f}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_calibration_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "calibration"}) as run:
        log_drift_run(
            run,
            params={
                'experiment': 'calibration',
                'seed': seed,
                'alpha': alpha,
                'n_sims': n_sims,
                'n_per_window': n_per_window,
                'n_permutations': n_permutations,
                'git_hash': get_git_hash(),
            },
            metrics={
                'fpr_ks_raw': fpr_rows[0]['fpr_raw'],
                'fpr_ks_bh': fpr_rows[0]['fpr_bh'],
                'fpr_z_raw': fpr_rows[1]['fpr_raw'],
                'fpr_z_bh': fpr_rows[1]['fpr_bh'],
                'fpr_mmd_raw': fpr_rows[2]['fpr_raw'],
                'fpr_mmd_bh': fpr_rows[2]['fpr_bh'],
                'fpr_any_raw': fpr_rows[3]['fpr_raw'],
                'fpr_any_bh': fpr_rows[3]['fpr_bh'],
            },
            artifact_files=[hist_path, fpr_csv, csv_path]
        )

    return {'n_sims': n_sims, 'fpr': fpr_rows}


def main():
    parser = argparse.ArgumentParser(description='Calibration of drift tests under H0')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-sims', type=int, default=1000)
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--n-per-window', type=int, default=50)
    parser.add_argument('--n-permutations', type=int, default=100)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(
        n_sims=args.n_sims,
        seed=args.seed,
        alpha=args.alpha,
        n_per_window=args.n_per_window,
        n_permutations=args.n_permutations,
        out_dir=args.out_dir
    )


if __name__ == '__main__':
    main()
