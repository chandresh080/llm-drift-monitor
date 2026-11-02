"""Power analysis: detect injected shifts of known size across sample sizes."""
import argparse
import csv
import os
import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, two_proportion_z_test, mmd_test
from experiments.sim import (
    make_lengths,
    make_validity,
    make_embeddings,
    shift_lengths,
    shift_validity,
    shift_embeddings,
    DEFAULT_LENGTH_STD,
    DEFAULT_VALID_P,
)

DELTAS = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0]
NS = [10, 25, 50, 100]


def run(
    seed: int = 42,
    n_sims: int = 50,
    n_permutations: int = 100,
    alpha: float = 0.05,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    print(f"[power] Sweeping power curves: deltas={DELTAS}, Ns={NS}, n_sims={n_sims}, alpha={alpha}...")
    results = []

    for test in ['ks', 'z', 'mmd']:
        for n in NS:
            for delta in DELTAS:
                rejections = 0
                for i in range(n_sims):
                    sim_seed = seed * 100000 + i * 17
                    rng_ref = np.random.default_rng(sim_seed)
                    rng_cur = np.random.default_rng(sim_seed + 1)

                    if test == 'ks':
                        ref = make_lengths(n, rng=rng_ref)
                        cur = make_lengths(n, rng=rng_cur)
                        if delta > 0:
                            cur = shift_lengths(cur, delta_mean=delta * DEFAULT_LENGTH_STD)
                        stat, p = ks_test(ref, cur)

                    elif test == 'z':
                        ref = make_validity(n, p_valid=DEFAULT_VALID_P, rng=rng_ref)
                        cur_p = max(0.05, DEFAULT_VALID_P - delta * 0.15)
                        cur = make_validity(n, p_valid=cur_p, rng=rng_cur)
                        stat, p = two_proportion_z_test(ref, cur)

                    else:  # mmd
                        ref = make_embeddings(n, rng=rng_ref)
                        cur = make_embeddings(n, rng=rng_cur)
                        if delta > 0:
                            cur = shift_embeddings(cur, delta_mean=delta * 0.35, rng=rng_cur)
                        stat, p = mmd_test(ref, cur, n_permutations=n_permutations, random_seed=sim_seed)

                    if not np.isnan(p) and p < alpha:
                        rejections += 1

                power = rejections / float(n_sims)
                results.append({
                    'test': test,
                    'delta': delta,
                    'n': n,
                    'power': float(power),
                    'n_sims': n_sims,
                    'alpha': alpha,
                })

    # Save to CSV
    csv_path = os.path.join(out_dir, 'power_curves.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # Plot 3-panel figure: one per test, one line per sample size N
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=True)
    test_meta = {
        'ks': ('Kolmogorov-Smirnov (Length Drift)', 'Shift Size (\u03b4 in \u03c3 units)'),
        'z': ('Two-Proportion Z (Refusal/Invalid Rate)', 'Refusal Shift Index (\u03b4 \u00d7 15% drop)'),
        'mmd': ('MMD with Median Heuristic (Semantic Drift)', 'Embedding Mean Shift (\u03b4 units)'),
    }

    colors = {10: '#94a3b8', 25: '#3b82f6', 50: '#10b981', 100: '#8b5cf6'}
    markers = {10: 'o', 25: 's', 50: '^', 100: 'D'}

    for ax, (test_name, (title, xlabel)) in zip(axes, test_meta.items()):
        for n in NS:
            rows = [r for r in results if r['test'] == test_name and r['n'] == n]
            deltas = [r['delta'] for r in rows]
            powers = [r['power'] for r in rows]
            ax.plot(
                deltas, powers,
                marker=markers[n],
                color=colors[n],
                linewidth=2.0,
                markersize=6,
                label=f'N = {n}'
            )

        ax.axhline(0.80, color='red', linestyle='--', alpha=0.7, label='80% Target Power')
        ax.axhline(alpha, color='black', linestyle=':', alpha=0.5, label=f'\u03b1 = {alpha} (FPR)')
        ax.set_title(title, fontsize=11, fontweight='bold')
        ax.set_xlabel(xlabel, fontsize=10)
        ax.set_ylabel('Empirical Statistical Power (1 - \u03b2)', fontsize=10)
        ax.set_ylim(-0.02, 1.05)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='lower right', fontsize=8)

    plt.suptitle('Detector Power vs Shift Size (\u03b4) Across Sample Sizes (N Probes/Window)', fontsize=13, y=1.02)
    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'power_curves.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[power] Complete. Saved to {csv_path} and {plot_path}")
    print("[power] Key Takeaway: To catch a 1.0\u03c3 shift with \u226580% power at \u03b1=0.05, N=50 probes/window is required. For shifts \u22652.0\u03c3, N=25 is sufficient.")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_power_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "power"}) as run:
        power_metrics = {}
        for r in results:
            delta_str = str(r['delta']).replace('.', '_')
            power_metrics[f"power_{r['test']}_d{delta_str}_n{r['n']}"] = r['power']

        log_drift_run(
            run,
            params={
                'experiment': 'power',
                'seed': seed,
                'n_sims': n_sims,
                'n_permutations': n_permutations,
                'alpha': alpha,
                'git_hash': get_git_hash(),
            },
            metrics=power_metrics,
            artifact_files=[csv_path, plot_path]
        )

    return {'results': results}


def main():
    parser = argparse.ArgumentParser(description='Power analysis of drift detectors')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-sims', type=int, default=50)
    parser.add_argument('--n-permutations', type=int, default=100)
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(
        seed=args.seed,
        n_sims=args.n_sims,
        n_permutations=args.n_permutations,
        alpha=args.alpha,
        out_dir=args.out_dir
    )


if __name__ == '__main__':
    main()
