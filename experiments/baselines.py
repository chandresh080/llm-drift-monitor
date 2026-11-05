"""Detector baseline comparison: does MMD earn its complexity?

Two controlled scenarios on synthetic data where the truth is known:

1. Semantic mean shift: current embeddings move along a fixed direction.
   Compares MMD against the centroid distance baseline (a mean based monitor)
   and the KS test, all at the same nominal 5 per cent false alarm rate.
2. Variance shift: current embeddings spread out while the mean stays put.
   The centroid distance is blind to this by construction, which is the whole
   reason the kernel test exists; this scenario quantifies the gap.

Usage:
    python -m experiments.baselines --shifts 0.0 0.2 0.4 --sims 200
"""

import argparse
import csv
import os

import numpy as np

from drift_tests import ks_test, mmd_test, centroid_distance
from experiments.sim import (make_embeddings, make_lengths, shift_embeddings,
                             shift_lengths, DEFAULT_LENGTH_STD)

ALPHA = 0.05


def _null_centroid_threshold(n_ref: int, n_cur: int, seed: int) -> float:
    """Calibrate the centroid statistic on null data at the 95th percentile,
    so its detection rate is comparable to the tests that have p values."""
    rng = np.random.default_rng(seed)
    distances = [centroid_distance(make_embeddings(n_ref, rng=rng),
                                   make_embeddings(n_cur, rng=rng))
                 for _ in range(200)]
    return float(np.quantile(distances, 0.95))


def _power(detected: int, n_sims: int) -> float:
    return round(detected / n_sims, 3)


def run(shifts=None, n_sims: int = 200, n_ref: int = 50, n_cur: int = 10,
        seed: int = 42, n_permutations: int = 300, out_dir: str = None) -> dict:
    shifts = shifts or [0.0, 0.2, 0.4]
    rows = []
    centroid_threshold = _null_centroid_threshold(n_ref, n_cur, seed)
    for shift in shifts:
        detected = {"ks": 0, "mmd": 0, "centroid_mean": 0, "centroid_var": 0}
        for i in range(n_sims):
            rng = np.random.default_rng(seed + 1000 + i)
            # Scenario 1: semantic mean shift along a fixed direction.
            x = make_embeddings(n_ref, rng=rng)
            y = shift_embeddings(x.copy(), shift, rng=rng)
            _, mmd_p = mmd_test(x.tolist(), y.tolist(),
                                n_permutations=n_permutations,
                                random_seed=seed + i)
            # A length shift of the same magnitude in standard units gives KS
            # its best comparable shot (the verbosity scenario).
            l_ref = make_lengths(n_ref, rng=rng)
            _, ks_p = ks_test(list(l_ref),
                              list(shift_lengths(l_ref, shift * DEFAULT_LENGTH_STD)))
            # Scenario 2: pure variance shift, mean unchanged.
            y_spread = x[:n_cur] + np.asarray(
                rng.normal(0.0, 0.5, size=(n_cur, x.shape[1])))
            norms = np.linalg.norm(y_spread, axis=-1, keepdims=True)
            y_spread = y_spread / np.maximum(norms, 1e-12)

            if ks_p < ALPHA:
                detected["ks"] += 1
            if mmd_p < ALPHA:
                detected["mmd"] += 1
            if centroid_distance(x, y) > centroid_threshold:
                detected["centroid_mean"] += 1
            if centroid_distance(x, y_spread) > centroid_threshold:
                detected["centroid_var"] += 1
        rows.append({
            "shift": shift,
            "ks_power_length_shift": _power(detected["ks"], n_sims),
            "mmd_power_mean_shift": _power(detected["mmd"], n_sims),
            "centroid_power_mean_shift": _power(detected["centroid_mean"], n_sims),
            "centroid_power_variance_shift": _power(detected["centroid_var"], n_sims),
        })
        print(f"shift={shift:.1f}  ks={rows[-1]['ks_power_length_shift']}  "
              f"mmd={rows[-1]['mmd_power_mean_shift']}  "
              f"centroid(mean)={rows[-1]['centroid_power_mean_shift']}  "
              f"centroid(var)={rows[-1]['centroid_power_variance_shift']}")

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "baselines_power.csv")
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {path}")
    return {"rows": rows, "n_sims": n_sims}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shifts", type=float, nargs="+",
                        default=[0.0, 0.2, 0.4])
    parser.add_argument("--sims", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out-dir", default="results")
    args = parser.parse_args()
    run(shifts=args.shifts, n_sims=args.sims, seed=args.seed, out_dir=args.out_dir)


if __name__ == "__main__":
    main()
