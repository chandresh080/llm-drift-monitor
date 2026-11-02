"""Sensitivity: embedding model / kernel bandwidth / permutation-count sensitivity."""
import argparse
import csv
import os

import matplotlib.pyplot as plt
import numpy as np

from drift_tests import mmd_test
from experiments.sim import make_embeddings, shift_embeddings


def run(seed: int = 42, out_dir=None) -> dict:
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'results')
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for emb_name, mean, std in [('minilm', 0.0, 1.0), ('mpnet', 0.0, 2.0)]:
        for sigma in [0.1, 1.0, 10.0]:
            for perm in [200, 1000]:
                ref = make_embeddings(50, mean=mean, std=std, rng=np.random.default_rng(seed))
                cur = shift_embeddings(make_embeddings(50, mean=mean, std=std, rng=np.random.default_rng(seed)), 1.0, std=std)
                stat, p = mmd_test(ref, cur, n_permutations=perm, random_seed=42, sigma=sigma)
                results.append({
                    'embedding_model': emb_name,
                    'sigma': sigma,
                    'permutations': perm,
                    'mmd_stat': float(stat),
                    'p_value': float(p),
                    'rejected': bool(p < 0.05),
                })

    csv_path = os.path.join(out_dir, 'sensitivity.csv')
    with open(csv_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    fig, ax = plt.subplots(figsize=(8, 5))
    for emb_name in ['minilm', 'mpnet']:
        rows = [r for r in results if r['embedding_model'] == emb_name and r['permutations'] == 1000]
        if rows:
            ax.plot([r['sigma'] for r in rows], [r['p_value'] for r in rows], marker='o', label=f'{emb_name} (perm=1000)')
    ax.set_xlabel('Kernel sigma')
    ax.set_ylabel('MMD p-value')
    ax.set_title('Sensitivity: MMD p-value across embedding model / sigma / permutations')
    ax.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(out_dir, 'sensitivity.png'))
    plt.close()

    return {'results': results}


def main():
    parser = argparse.ArgumentParser(description='Sensitivity analysis')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    out = run(seed=args.seed)
    print('Sensitivity complete:', len(out['results']), 'combos')


if __name__ == '__main__':
    main()
