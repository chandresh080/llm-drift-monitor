"""Localization: per-category shift detection with BH correction."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import ks_test, benjamini_hochberg_correction
from experiments.sim import make_lengths, shift_lengths


CATEGORIES = ['factual', 'reasoning', 'code', 'refusal']
PER_CAT_N = 25
SHIFT_DELTA = 2.0  # in std units of length


def run(seed: int = 42, out_dir=None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    per_cat = []
    for c, cat in enumerate(CATEGORIES):
        rng = np.random.default_rng(seed + c)
        ref = make_lengths(PER_CAT_N, rng=rng)
        cur = make_lengths(PER_CAT_N, rng=rng)
        shifted = False
        if c == 0:
            cur = shift_lengths(cur, SHIFT_DELTA * 15.0)
            shifted = True
        stat, p = ks_test(ref, cur)
        per_cat.append({'category': cat, 'shifted': shifted, 'p': float(p), 'rejected': bool(p < 0.05)})

    adj, sig = benjamini_hochberg_correction([r['p'] for r in per_cat], alpha=0.05)
    flagged = [per_cat[i]['category'] for i in range(len(per_cat)) if sig[i]]

    shifted_cats = [CATEGORIES[0]]
    tp = len(set(flagged) & set(shifted_cats))
    precision = tp / len(flagged) if flagged else 0.0
    recall = tp / len(shifted_cats)

    csv_path = os.path.join(out_dir, 'localization_results.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(per_cat[0].keys()))
        w.writeheader()
        w.writerows(per_cat)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar([r['category'] for r in per_cat], [-np.log10(r['p']) for r in per_cat])
    for r in per_cat:
        if r['rejected']:
            ax.axvline(list(['factual', 'reasoning', 'code', 'refusal']).index(r['category']), color='red', linestyle='--')
    ax.set_xlabel('Category')
    ax.set_ylabel('-log10(p)')
    ax.set_title('Localization: per-category p-values (shifted category marked)')
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, 'localization.png'))
    plt.close()

    return {'flagged': flagged, 'precision': precision, 'recall': recall}


def main():
    parser = argparse.ArgumentParser(description='Localization: per-category drift detection')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    out = run(seed=args.seed)
    print('Localization complete:', out)


if __name__ == '__main__':
    main()
