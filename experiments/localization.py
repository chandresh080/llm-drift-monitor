"""Localization: per-category drift detection with Benjamini-Hochberg FDR control."""
import argparse
import csv
import os
import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, benjamini_hochberg_correction
from experiments.sim import make_lengths, shift_lengths, CATEGORIES, DEFAULT_LENGTH_STD

PER_CAT_N = 30
SHIFT_DELTA_SIGMA = 1.5


def run(
    seed: int = 42,
    n_sims: int = 100,
    target_category: str = 'code',
    alpha: float = 0.05,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    if target_category not in CATEGORIES:
        target_category = CATEGORIES[2]  # 'code'

    print(f"[localization] Evaluating category shift localization over {n_sims} trials (shifted category: '{target_category}')...")

    category_detections = {cat: 0 for cat in CATEGORIES}
    precisions = []
    recalls = []

    for i in range(n_sims):
        sim_seed = seed * 1000 + i * 31
        cat_pvals = []

        for c_idx, cat in enumerate(CATEGORIES):
            rng_ref = np.random.default_rng(sim_seed + c_idx * 2)
            rng_cur = np.random.default_rng(sim_seed + c_idx * 2 + 1)

            ref = make_lengths(PER_CAT_N, rng=rng_ref)
            cur = make_lengths(PER_CAT_N, rng=rng_cur)

            # Inject shift only in the target category
            if cat == target_category:
                cur = shift_lengths(cur, delta_mean=SHIFT_DELTA_SIGMA * DEFAULT_LENGTH_STD)

            _, p = ks_test(ref, cur)
            cat_pvals.append(p)

        # Benjamini-Hochberg correction across the 5 categories
        adjusted_pvals, significant = benjamini_hochberg_correction(cat_pvals, alpha=alpha)

        flagged_cats = [cat for cat, sig in zip(CATEGORIES, significant) if sig]
        for f_cat in flagged_cats:
            category_detections[f_cat] += 1

        # Calculate trial precision and recall
        tp = 1 if target_category in flagged_cats else 0
        fp = len(flagged_cats) - tp
        precision = (tp / len(flagged_cats)) if len(flagged_cats) > 0 else 1.0 if tp == 0 and len(flagged_cats) == 0 else 0.0
        # If nothing flagged, precision is considered 1.0 (no false alarms) or 0.0
        prec_metric = (tp / len(flagged_cats)) if len(flagged_cats) > 0 else 0.0
        recall = float(tp)  # since exactly 1 shifted category

        precisions.append(prec_metric)
        recalls.append(recall)

    # Compile results
    rows = []
    for cat in CATEGORIES:
        det_count = category_detections[cat]
        det_rate = det_count / float(n_sims)
        rows.append({
            'category': cat,
            'is_shifted': int(cat == target_category),
            'n_sims': n_sims,
            'detections': det_count,
            'detection_rate': round(det_rate, 4),
            'alpha': alpha,
        })

    # Summary row
    mean_precision = float(np.mean(precisions))
    mean_recall = float(np.mean(recalls))
    f1 = (2 * mean_precision * mean_recall) / (mean_precision + mean_recall) if (mean_precision + mean_recall) > 0 else 0.0

    summary_row = {
        'category': 'OVERALL_METRICS',
        'is_shifted': -1,
        'n_sims': n_sims,
        'detections': -1,
        'detection_rate': round(mean_recall, 4),
        'alpha': alpha,
        'mean_precision': round(mean_precision, 4),
        'mean_recall': round(mean_recall, 4),
        'f1_score': round(f1, 4),
    }

    csv_path = os.path.join(out_dir, 'localization_results.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['category', 'is_shifted', 'n_sims', 'detections', 'detection_rate', 'alpha', 'mean_precision', 'mean_recall', 'f1_score'], restval='')
        w.writeheader()
        for r in rows:
            w.writerow(r)
        w.writerow(summary_row)

    # Plot
    fig, ax = plt.subplots(figsize=(8.5, 5))
    cats = [r['category'] for r in rows]
    rates = [r['detection_rate'] * 100.0 for r in rows]
    bar_colors = ['#ef4444' if c == target_category else '#3b82f6' for c in cats]

    bars = ax.bar(cats, rates, color=bar_colors, alpha=0.85, edgecolor='black', width=0.55)
    for bar, rate in zip(bars, rates):
        ax.text(bar.get_x() + bar.get_width() / 2.0, rate + 1.5, f'{rate:.1f}%', ha='center', va='bottom', fontsize=10, fontweight='bold')

    ax.axhline(alpha * 100.0, color='gray', linestyle='--', linewidth=1.2, label=f'Nominal FPR limit (\u03b1={alpha*100:.0f}%)')
    ax.set_title(f'Category Drift Localization under Single-Category Shift (Shifted: {target_category})', fontsize=11, fontweight='bold')
    ax.set_xlabel('Canary Prompt Category', fontsize=10)
    ax.set_ylabel('Empirical Detection Rate (%) Across 100 Trials', fontsize=10)
    ax.set_ylim(0, 110)
    ax.grid(True, alpha=0.3, axis='y')
    ax.legend(loc='upper right', fontsize=9)

    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'localization.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[localization] Complete. Saved to {csv_path} and {plot_path}")
    print(f"[localization] Precision: {mean_precision:.3f} | Recall: {mean_recall:.3f} | F1: {f1:.3f}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_localization_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "localization"}) as run:
        log_drift_run(
            run,
            params={
                'experiment': 'localization',
                'seed': seed,
                'n_sims': n_sims,
                'target_category': target_category,
                'alpha': alpha,
                'git_hash': get_git_hash(),
            },
            metrics={
                'precision': mean_precision,
                'recall': mean_recall,
                'f1_score': f1,
                'detection_rate_shifted': float([r['detection_rate'] for r in rows if r['category'] == target_category][0]),
            },
            artifact_files=[csv_path, plot_path]
        )

    return {'mean_precision': mean_precision, 'mean_recall': mean_recall, 'f1': f1}


def main():
    parser = argparse.ArgumentParser(description='Category drift localization with BH FDR control')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-sims', type=int, default=100)
    parser.add_argument('--target-category', type=str, default='code')
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(
        seed=args.seed,
        n_sims=args.n_sims,
        target_category=args.target_category,
        alpha=args.alpha,
        out_dir=args.out_dir
    )


if __name__ == '__main__':
    main()
