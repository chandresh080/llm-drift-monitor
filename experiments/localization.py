"""Localization: does category-wise MMD point at the right category?

This experiment runs the exact procedure the monitor runs at runtime
(draft_tests.category_mmd_tests: per category permutation MMD over matched
embedding windows, Benjamini Hochberg correction across the tested family),
not a stand-in. Embedding distributions are synthesized for all five
categories, a shift is injected into one target category, and precision,
recall and F1 of the flagged-category set are measured across repeated
trials. A separate null scenario (no category shifted) reports how often the
procedure flags anything at all.
"""
import argparse
import csv
import os
import matplotlib.pyplot as plt
import numpy as np

from drift_tests import category_mmd_tests
from experiments.sim import CATEGORIES, EMBEDDING_DIM

PER_CAT_N = 30
# Shift size in std units along a random direction. Chosen so the procedure's
# localization (not just MMD's raw power) is what gets measured: below about
# 3 the pooled test is near its power floor at this sample size and recall
# collapses regardless of localization quality.
SHIFT_DELTA_SIGMA = 3.5
# Per category sample count matches the monitor's category windows
# (5 balanced cycles of 10 probes give 10 per category); 30 makes the
# experiment slightly harder than production rather than easier.
MIN_PER_CATEGORY = 8


def _sim_category_embeddings(sim_seed: int, target_category, shift: float):
    """Reference and current embeddings per category, shift in the target."""
    ref_by_cat, cur_by_cat = {}, {}
    for c_idx, cat in enumerate(CATEGORIES):
        rng_ref = np.random.default_rng(sim_seed + c_idx * 2)
        rng_cur = np.random.default_rng(sim_seed + c_idx * 2 + 1)
        ref = rng_ref.normal(0.0, 1.0, size=(PER_CAT_N, EMBEDDING_DIM))
        cur = rng_cur.normal(0.0, 1.0, size=(PER_CAT_N, EMBEDDING_DIM))
        if cat == target_category and shift:
            direction = np.random.default_rng(sim_seed + 999).normal(size=EMBEDDING_DIM)
            direction /= np.linalg.norm(direction)
            cur = cur + shift * direction
        ref_by_cat[cat] = (ref / np.linalg.norm(ref, axis=-1, keepdims=True)).tolist()
        cur_by_cat[cat] = (cur / np.linalg.norm(cur, axis=-1, keepdims=True)).tolist()
    return ref_by_cat, cur_by_cat


def run(
    seed: int = 42,
    n_sims: int = 100,
    target_category: str = 'code',
    alpha: float = 0.05,
    shift: float = SHIFT_DELTA_SIGMA,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    if target_category not in CATEGORIES:
        target_category = CATEGORIES[2]  # 'code'

    print(f"[localization] Evaluating category-wise MMD localization over "
          f"{n_sims} trials (shifted category: '{target_category}')...")

    category_detections = {cat: 0 for cat in CATEGORIES}
    precisions = []
    recalls = []
    null_flag_rates = []

    for i in range(n_sims):
        sim_seed = seed * 1000 + i * 31

        # Shifted trial: run the monitor's category-wise MMD procedure.
        ref_by_cat, cur_by_cat = _sim_category_embeddings(sim_seed, target_category, shift)
        result = category_mmd_tests(
            ref_by_cat, cur_by_cat,
            min_samples_per_category=MIN_PER_CATEGORY, alpha=alpha,
            random_seed=sim_seed)
        flagged_cats = [cat for cat, t in result.items() if t.get('significant')]
        for f_cat in flagged_cats:
            category_detections[f_cat] += 1

        tp = 1 if target_category in flagged_cats else 0
        prec_metric = (tp / len(flagged_cats)) if len(flagged_cats) > 0 else 0.0
        recall = float(tp)  # exactly one shifted category
        precisions.append(prec_metric)
        recalls.append(recall)

        # Null trial with the same seeds: no category shifted. Counts toward
        # the localization false alarm rate (any category flagged).
        null_ref, null_cur = _sim_category_embeddings(sim_seed, None, 0.0)
        null_result = category_mmd_tests(
            null_ref, null_cur,
            min_samples_per_category=MIN_PER_CATEGORY, alpha=alpha,
            random_seed=sim_seed)
        null_flag_rates.append(
            1.0 if any(t.get('significant') for t in null_result.values()) else 0.0)

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
    null_flag_rate = float(np.mean(null_flag_rates))

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
        'null_flag_rate': round(null_flag_rate, 4),
    }

    csv_path = os.path.join(out_dir, 'localization_results.csv')
    fieldnames = ['category', 'is_shifted', 'n_sims', 'detections', 'detection_rate',
                  'alpha', 'mean_precision', 'mean_recall', 'f1_score', 'null_flag_rate']
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, restval='')
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
    ax.set_title(f'Category-wise MMD Localization, Single-Category Shift (Shifted: {target_category})', fontsize=11, fontweight='bold')
    ax.set_xlabel('Canary Prompt Category', fontsize=10)
    ax.set_ylabel('Empirical Detection Rate (%) Across Trials', fontsize=10)
    ax.set_ylim(0, 110)
    ax.grid(True, alpha=0.3, axis='y')
    ax.legend(loc='upper right', fontsize=9)

    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'localization.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[localization] Complete. Saved to {csv_path} and {plot_path}")
    print(f"[localization] Precision: {mean_precision:.3f} | Recall: {mean_recall:.3f} | "
          f"F1: {f1:.3f} | null flag rate: {null_flag_rate:.3f}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_localization_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "localization"}) as run:
        log_drift_run(
            run,
            params={
                'experiment': 'localization',
                'procedure': 'category_mmd_tests',
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
                'null_flag_rate': null_flag_rate,
            },
            artifact_files=[csv_path, plot_path]
        )

    return {'mean_precision': mean_precision, 'mean_recall': mean_recall,
            'f1': f1, 'null_flag_rate': null_flag_rate}


def main():
    parser = argparse.ArgumentParser(description='Category-wise MMD localization with BH correction')
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
