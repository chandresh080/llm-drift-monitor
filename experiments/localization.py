"""Localization: does category-wise MMD point at the right category?

This experiment runs the exact procedure the monitor runs at runtime
(drift_tests.category_mmd_tests: per category permutation MMD over matched
embedding windows, Benjamini Hochberg correction across the tested family),
at the production category sample count. The monitor's category windows hold
five cycles of two prompts per category, which is 10 samples per category,
so that is what is simulated here: PER_CAT_N matches production, not a
larger, easier sample size.

For each shift magnitude on a grid, a shift is injected into one target
category and precision, recall and F1 of the flagged-category set are
measured across repeated trials, alongside a null scenario (no category
shifted) that reports how often the procedure flags anything at all.
"""
import argparse
import csv
import os
import matplotlib.pyplot as plt
import numpy as np

from drift_tests import category_mmd_tests
from experiments.sim import CATEGORIES, EMBEDDING_DIM

# Production parity: the monitor's category windows hold 10 samples per
# category (5 cycles x 2 balanced prompts). Simulating more samples would
# make detection easier than it is in production, not harder.
PER_CAT_N = 10
MIN_PER_CATEGORY = 8
SHIFT_GRID = [0.5, 1.0, 1.5, 2.0, 3.0]


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
    shifts=None,
    out_dir: str = None
) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    if target_category not in CATEGORIES:
        target_category = CATEGORIES[2]  # 'code'
    shifts = shifts or list(SHIFT_GRID)

    print(f"[localization] Evaluating category-wise MMD localization over "
          f"{n_sims} trials at {PER_CAT_N} samples per category (production "
          f"parity), shifted category '{target_category}', shifts {shifts}...")

    rows = []
    summaries = []
    for shift in shifts:
        category_detections = {cat: 0 for cat in CATEGORIES}
        precisions = []
        recalls = []
        null_flag_rates = []

        for i in range(n_sims):
            sim_seed = seed * 1000 + i * 31

            # Shifted trial: run the monitor's category-wise MMD procedure.
            ref_by_cat, cur_by_cat = _sim_category_embeddings(
                sim_seed, target_category, shift)
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

            # Null trial with the same seeds: no category shifted. Counts
            # toward the localization false alarm rate (any category flagged).
            null_ref, null_cur = _sim_category_embeddings(sim_seed, None, 0.0)
            null_result = category_mmd_tests(
                null_ref, null_cur,
                min_samples_per_category=MIN_PER_CATEGORY, alpha=alpha,
                random_seed=sim_seed)
            null_flag_rates.append(
                1.0 if any(t.get('significant') for t in null_result.values()) else 0.0)

        mean_precision = float(np.mean(precisions))
        mean_recall = float(np.mean(recalls))
        f1 = (2 * mean_precision * mean_recall) / (mean_precision + mean_recall) if (mean_precision + mean_recall) > 0 else 0.0
        null_flag_rate = float(np.mean(null_flag_rates))

        for cat in CATEGORIES:
            det_count = category_detections[cat]
            rows.append({
                'shift': shift,
                'category': cat,
                'is_shifted': int(cat == target_category),
                'n_sims': n_sims,
                'detections': det_count,
                'detection_rate': round(det_count / float(n_sims), 4),
                'alpha': alpha,
                'per_cat_samples': PER_CAT_N,
            })
        summaries.append({
            'shift': shift,
            'mean_precision': round(mean_precision, 4),
            'mean_recall': round(mean_recall, 4),
            'f1_score': round(f1, 4),
            'null_flag_rate': round(null_flag_rate, 4),
        })
        print(f"[localization] shift={shift:.1f}: precision={mean_precision:.3f} "
              f"recall={mean_recall:.3f} F1={f1:.3f} null flag={null_flag_rate:.3f}")

    csv_path = os.path.join(out_dir, 'localization_results.csv')
    fieldnames = ['shift', 'category', 'is_shifted', 'n_sims', 'detections',
                  'detection_rate', 'alpha', 'per_cat_samples', 'mean_precision',
                  'mean_recall', 'f1_score', 'null_flag_rate']
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, restval='')
        w.writeheader()
        for r in rows:
            w.writerow(r)
        for s in summaries:
            w.writerow({
                'shift': s['shift'], 'category': 'OVERALL_METRICS',
                'is_shifted': -1, 'n_sims': n_sims, 'detections': -1,
                'detection_rate': s['mean_recall'], 'alpha': alpha,
                'per_cat_samples': PER_CAT_N,
                'mean_precision': s['mean_precision'],
                'mean_recall': s['mean_recall'],
                'f1_score': s['f1_score'],
                'null_flag_rate': s['null_flag_rate'],
            })

    # Plot: recall and null flag rate across the shift grid.
    fig, ax = plt.subplots(figsize=(8.5, 5))
    xs = [s['shift'] for s in summaries]
    ax.plot(xs, [s['mean_recall'] * 100.0 for s in summaries], 'o-',
            color='#ef4444', label='Shifted-category recall')
    ax.plot(xs, [s['mean_precision'] * 100.0 for s in summaries], 's-',
            color='#3b82f6', label='Precision of flagged set')
    ax.axhline(100.0 * summaries[0]['null_flag_rate'], color='gray',
               linestyle='--', linewidth=1.2, label='Null any-category flag rate')
    ax.set_title('Category-wise MMD Localization at Production Sample Size '
                 f'({PER_CAT_N} per category)', fontsize=11, fontweight='bold')
    ax.set_xlabel('Injected shift (standard deviations along a fixed direction)', fontsize=10)
    ax.set_ylabel('Rate (%) across trials', fontsize=10)
    ax.set_ylim(-2, 105)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='lower right', fontsize=9)
    plt.tight_layout()
    plot_path = os.path.join(out_dir, 'localization.png')
    plt.savefig(plot_path, dpi=200, bbox_inches='tight')
    plt.close()

    print(f"[localization] Complete. Saved to {csv_path} and {plot_path}")

    from tracking import drift_run, log_drift_run, get_git_hash
    with drift_run(f"experiment_localization_seed{seed}", experiment_name="llm-drift-experiments", tags={"experiment": "localization"}) as run:
        log_drift_run(
            run,
            params={
                'experiment': 'localization',
                'procedure': 'category_mmd_tests',
                'seed': seed,
                'n_sims': n_sims,
                'per_cat_samples': PER_CAT_N,
                'target_category': target_category,
                'alpha': alpha,
                'shift_grid': ','.join(str(s) for s in shifts),
                'git_hash': get_git_hash(),
            },
            metrics={
                **{f'precision_shift_{s["shift"]}': s['mean_precision'] for s in summaries},
                **{f'recall_shift_{s["shift"]}': s['mean_recall'] for s in summaries},
                **{f'null_flag_rate_shift_{s["shift"]}': s['null_flag_rate'] for s in summaries},
            },
            artifact_files=[csv_path, plot_path]
        )

    return {'summaries': summaries}


def main():
    parser = argparse.ArgumentParser(description='Category-wise MMD localization with BH correction')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n-sims', type=int, default=100)
    parser.add_argument('--target-category', type=str, default='code')
    parser.add_argument('--alpha', type=float, default=0.05)
    parser.add_argument('--shifts', type=float, nargs='+', default=None)
    parser.add_argument('--out-dir', type=str, default=None)
    args = parser.parse_args()
    run(
        seed=args.seed,
        n_sims=args.n_sims,
        target_category=args.target_category,
        alpha=args.alpha,
        shifts=args.shifts,
        out_dir=args.out_dir
    )


if __name__ == '__main__':
    main()
