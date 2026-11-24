"""Monte Carlo calibration for the deployed 3:1 matched-prompt design.

This is an audit tool, not a production detector. It simulates the hosted
config/config.gemini.yaml windows: 40 fixed prompts, three reference cycles and
one current cycle, prompt-specific baselines, within-prompt variability, AR(1)
temporal correlation, and empty/failed responses. It compares:

  current : pooled KS lengths + Fisher/z availability + whole-cluster swap MMD
  paired  : paired-mean t lengths + within-prompt availability permutation
            + within-prompt allocation-preserving MMD

Both families use the same BH correction and the same alert persistence rule.
Run `python experiments/sampling_design_calibration.py --quick` for a smoke run,
or the default full run for calibration-grade estimates.

No production detector is changed by this script. Results are written to
results/statistical_audit/.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drift_tests import (  # noqa: E402
    _compute_kernel_matrix,
    benjamini_hochberg_correction,
    ks_test,
    paired_cluster_mmd_test,
    proportion_test,
)

ALPHA = 0.05


@dataclass(frozen=True)
class Shift:
    """Controlled shifts injected only into the current window."""

    length: float = 0.0
    emb_mean: float = 0.0
    emb_var: float = 1.0
    valid_drop: float = 0.0


@dataclass(frozen=True)
class SimConfig:
    prompts: int = 40
    ref_cycles: int = 3
    cur_cycles: int = 1
    emb_dim: int = 32
    prompt_emb_std: float = 0.70
    within_emb_std: float = 1.00
    temporal_emb: float = 0.0
    prompt_len_std: float = 15.0
    within_len_std: float = 8.0
    temporal_len: float = 0.0
    valid_alpha: float = 20.0
    valid_beta: float = 1.0
    failure_prompt_p: float = 0.25
    permutations: int = 999  # + identity = 1000 patterns, matching runtime
    alpha: float = ALPHA


@dataclass
class Cycle:
    emb: np.ndarray
    length: np.ndarray
    valid: np.ndarray


@dataclass
class Window:
    X: List[np.ndarray]
    Y: List[np.ndarray]
    xl: List[str]
    yl: List[str]
    lengths_ref: List[float]
    lengths_cur: List[float]
    valid_ref: List[int]
    valid_cur: List[int]
    lengths_ref_by_prompt: Dict[str, List[float]]
    lengths_cur_by_prompt: Dict[str, float]
    valid_ref_by_prompt: Dict[str, List[int]]
    valid_cur_by_prompt: Dict[str, List[int]]


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    arr = np.asarray(matrix, dtype=float)
    norms = np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-12)
    return arr / norms


def generate_panel(seed: int, cfg: SimConfig, shift: Shift, n_cycles: int,
                   shift_start: int) -> List[Cycle]:
    """Generate complete cycles with prompt effects and optional AR(1) noise."""
    rng = np.random.default_rng(seed)
    p = cfg.prompts
    base_emb = rng.normal(0.0, cfg.prompt_emb_std, size=(p, cfg.emb_dim))
    base_len = 100.0 + rng.normal(0.0, cfg.prompt_len_std, size=p)
    p_valid = np.clip(rng.beta(cfg.valid_alpha, cfg.valid_beta, size=p),
                      0.01, 0.999)
    # One deliberately failure-prone prompt, matching code_018 in the committed
    # Gemini reference database (three failed reference runs, one current run).
    p_valid[0] = cfg.failure_prompt_p
    shift_dir = rng.normal(size=cfg.emb_dim)
    shift_dir /= max(np.linalg.norm(shift_dir), 1e-12)

    rho = float(np.clip(cfg.temporal_emb / cfg.within_emb_std
                        if cfg.within_emb_std else 0.0, 0.0, 0.95))
    latent = np.empty((p, n_cycles), dtype=float)
    previous = rng.normal(size=p)
    for t in range(n_cycles):
        innovation = rng.normal(size=p)
        latent[:, t] = rho * previous + math.sqrt(1.0 - rho * rho) * innovation
        previous = latent[:, t]

    cycles: List[Cycle] = []
    for t in range(n_cycles):
        active = t >= shift_start
        raw = (base_emb
               + cfg.temporal_emb * latent[:, t, None]
               + cfg.within_emb_std * (shift.emb_var if active else 1.0)
               * rng.normal(size=(p, cfg.emb_dim)))
        if active and shift.emb_mean:
            # Scale by the noise-vector norm so emb_mean is interpretable as a
            # multiple of the within-prompt embedding noise magnitude.
            raw = raw + shift.emb_mean * math.sqrt(cfg.emb_dim) * shift_dir
        emb = _normalize_rows(raw)

        length = (base_len + cfg.temporal_len * latent[:, t]
                  + cfg.within_len_std * rng.normal(size=p))
        if active:
            length = length + shift.length
        length = np.maximum(length, 1.0).round()

        prob = np.clip(p_valid - (shift.valid_drop if active else 0.0),
                       0.01, 0.999)
        valid = rng.random(p) < prob
        cycles.append(Cycle(emb=emb, length=length, valid=valid))
    return cycles


def build_window(cycles: List[Cycle], ref_indices: List[int],
                 cur_index: int) -> Window:
    """Flatten cycles exactly as the runtime filters successful runs."""
    X: List[np.ndarray] = []
    Y: List[np.ndarray] = []
    xl: List[str] = []
    yl: List[str] = []
    lengths_ref: List[float] = []
    lengths_cur: List[float] = []
    valid_ref: List[int] = []
    valid_cur: List[int] = []
    lr: Dict[str, List[float]] = {}
    lc: Dict[str, float] = {}
    vr: Dict[str, List[int]] = {}
    vc: Dict[str, List[int]] = {}

    for p in range(cycles[0].valid.size):
        key = f"p{p}"
        vr[key] = []
        vc[key] = []
        for t in ref_indices:
            cyc = cycles[t]
            flag = int(cyc.valid[p])
            valid_ref.append(flag)
            vr[key].append(flag)
            if flag:
                X.append(cyc.emb[p])
                xl.append(key)
                lengths_ref.append(float(cyc.length[p]))
                lr.setdefault(key, []).append(float(cyc.length[p]))
        cyc = cycles[cur_index]
        flag = int(cyc.valid[p])
        valid_cur.append(flag)
        vc[key].append(flag)
        if flag:
            Y.append(cyc.emb[p])
            yl.append(key)
            lengths_cur.append(float(cyc.length[p]))
            lc[key] = float(cyc.length[p])

    return Window(X, Y, xl, yl, lengths_ref, lengths_cur, valid_ref,
                  valid_cur, lr, lc, vr, vc)


def single_window(seed: int, cfg: SimConfig, shift: Shift,
                  rho: float = 0.0) -> Window:
    temporal_cfg = SimConfig(**{**asdict(cfg),
                                "temporal_emb": rho * cfg.within_emb_std,
                                "temporal_len": rho * cfg.within_len_std})
    return build_window(
        generate_panel(seed, temporal_cfg, shift,
                       n_cycles=cfg.ref_cycles + cfg.cur_cycles,
                       shift_start=cfg.ref_cycles),
        list(range(cfg.ref_cycles)), cfg.ref_cycles)


# ---------------------------------------------------------------------------
# efficient exact-style permutation kernels
# ---------------------------------------------------------------------------


def mmd2_batch(K: np.ndarray, masks: np.ndarray) -> np.ndarray:
    """Unbiased MMD^2 for every reference-membership row in masks.

    Matches drift_tests._unbiased_mmd2_from_kernel, including the 0.0 returned
    when either group has fewer than two observations.
    """
    masks = np.asarray(masks, dtype=float)
    y = 1.0 - masks
    m = masks.sum(axis=1)
    n_y = masks.shape[1] - m
    ok = (m >= 2) & (n_y >= 2)
    out = np.zeros(masks.shape[0], dtype=float)
    if not np.any(ok):
        return out
    idx = np.flatnonzero(ok)
    sks = masks[idx] @ K @ masks[idx].T
    sys_ = y[idx] @ K @ y[idx].T
    sxy = masks[idx] @ K @ y[idx].T
    diag = np.diag(K)
    mi = m[idx]
    ni = n_y[idx]
    out[idx] = ((np.diag(sks) - masks[idx] @ diag) / (mi * (mi - 1.0))
                + (np.diag(sys_) - y[idx] @ diag) / (ni * (ni - 1.0))
                - 2.0 * np.diag(sxy) / (mi * ni))
    return out


def _pattern_masks(base: np.ndarray, pair_of_row: np.ndarray,
                   patterns: List[int]) -> np.ndarray:
    out = np.tile(base, (len(patterns), 1))
    for j, pattern in enumerate(patterns[1:], start=1):
        flip = np.array([(pattern >> k) & 1
                         for k in pair_of_row], dtype=bool)
        out[j, flip] = ~out[j, flip]
    return out


def cluster_swap_mmd(data: Window, cfg: SimConfig) -> Tuple[float, float, int]:
    """Fast equivalent of production paired_cluster_mmd_test.

    Uses the production RNG pattern sequence so sampled patterns match exactly;
    only the statistic evaluation is batched.
    """
    common = sorted(set(data.xl) & set(data.yl))
    if not common:
        return float("nan"), 1.0, 0
    pair_id = {key: k for k, key in enumerate(common)}
    keep_x = [i for i, key in enumerate(data.xl) if key in pair_id]
    keep_y = [i for i, key in enumerate(data.yl) if key in pair_id]
    nx, ny = len(keep_x), len(keep_y)
    if nx < 2 or ny < 2:
        return float("nan"), 1.0, len(common)

    X = np.asarray([data.X[i] for i in keep_x], dtype=float)
    Y = np.asarray([data.Y[i] for i in keep_y], dtype=float)
    Z = np.vstack([X, Y])
    K, _ = _compute_kernel_matrix(Z)

    base = np.ones(nx + ny, dtype=bool)
    base[nx:] = False
    pair_of_row = np.array(
        [pair_id[data.xl[i]] for i in keep_x]
        + [pair_id[data.yl[j]] for j in keep_y], dtype=int)

    if (1 << len(common)) <= 4096:
        patterns = list(range(1 << len(common)))
    else:
        rng = np.random.default_rng(42)
        sample = rng.integers(0, 1 << len(common), size=cfg.permutations)
        patterns = [0] + [int(v) for v in dict.fromkeys(sample.tolist())]
    masks = _pattern_masks(base, pair_of_row, patterns)
    stats_ = mmd2_batch(K, masks)
    obs = float(stats_[0])
    p = float(np.count_nonzero(stats_ >= obs - 1e-12) / len(stats_))
    return obs, min(1.0, max(0.0, p)), len(common)


def _choose_current_masks(row_groups: List[List[int]],
                          observed_current: List[List[int]],
                          n_patterns: int, seed: int) -> np.ndarray:
    """Reference masks preserving each prompt's observed group size.

    Row groups may be non-contiguous: the runtime flattens every reference row
    first and every current row second, so a prompt's rows are split across the
    two blocks. Random draws and the observed pattern are built from explicit
    row indices rather than assuming adjacency.
    """
    total = sum(len(group) for group in row_groups)
    masks = np.ones((n_patterns, total), dtype=bool)
    rng = np.random.default_rng(seed)
    rows = np.arange(n_patterns)[:, None]
    for group, current in zip(row_groups, observed_current):
        if not current:
            continue
        idx = np.asarray(group, dtype=int)
        chosen = np.argpartition(rng.random((n_patterns, idx.size)),
                                 len(current) - 1, axis=1)[:, :len(current)]
        masks[rows, idx[chosen]] = False
    # First pattern is the observed assignment, not a random draw.
    masks[0, :] = True
    for current in observed_current:
        masks[0, current] = False
    return masks


def allocation_mmd(data: Window, cfg: SimConfig,
                   seed: int = 7) -> Tuple[float, float, int]:
    """Within-prompt permutation preserving the 3:1 allocation."""
    common = sorted(set(data.xl) & set(data.yl))
    if not common:
        return float("nan"), 1.0, 0
    keep_x = [i for i, key in enumerate(data.xl) if key in common]
    keep_y = [i for i, key in enumerate(data.yl) if key in common]
    nx, ny = len(keep_x), len(keep_y)
    if nx < 2 or ny < 2:
        return float("nan"), 1.0, len(common)

    labels = ([data.xl[i] for i in keep_x]
              + [data.yl[j] for j in keep_y])
    Z = np.vstack([np.asarray([data.X[i] for i in keep_x], dtype=float),
                   np.asarray([data.Y[i] for i in keep_y], dtype=float)])
    K, _ = _compute_kernel_matrix(Z)

    row_groups = [[i for i, label in enumerate(labels) if label == key]
                  for key in common]
    observed_current = [[i for i in group if i >= nx]
                        for group in row_groups]
    if any(len(group) < 2 for group in row_groups) or \
            any(not current for current in observed_current):
        return float("nan"), 1.0, len(common)
    masks = _choose_current_masks(row_groups, observed_current,
                                  cfg.permutations + 1, seed)
    stats_ = mmd2_batch(K, masks)
    obs = float(stats_[0])
    p = float(np.count_nonzero(stats_ >= obs - 1e-12) / len(stats_))
    return obs, min(1.0, max(0.0, p)), len(common)


def paired_mean_length_test(data: Window) -> Tuple[float, float]:
    """Paired t on per-prompt mean reference length minus current length."""
    diffs = []
    for key, cur in data.lengths_cur_by_prompt.items():
        ref = data.lengths_ref_by_prompt.get(key) or []
        if ref:
            diffs.append(float(np.mean(ref)) - float(cur))
    if len(diffs) < 2:
        return float("nan"), 1.0
    arr = np.asarray(diffs, dtype=float)
    mean = float(arr.mean())
    sd = float(arr.std(ddof=1))
    if sd <= 1e-12:
        return mean, 0.0 if abs(mean) > 1e-12 else 1.0
    t_stat = mean / (sd / math.sqrt(len(arr)))
    p = 2.0 * stats.t.sf(abs(t_stat), df=len(arr) - 1)
    return t_stat, float(min(1.0, max(0.0, p)))


def paired_validity_permutation(data: Window, cfg: SimConfig,
                                seed: int = 11) -> Tuple[float, float]:
    """Within-prompt randomization of validity, preserving 3:1 allocation."""
    common = sorted(set(data.valid_ref_by_prompt)
                    & set(data.valid_cur_by_prompt))
    counts: List[int] = []
    b_obs: List[int] = []
    values: List[np.ndarray] = []
    for key in common:
        ref = np.asarray(data.valid_ref_by_prompt[key], dtype=float)
        cur = np.asarray(data.valid_cur_by_prompt[key], dtype=float)
        if ref.size == 0 or cur.size == 0:
            continue
        counts.append(ref.size + cur.size)
        b_obs.append(cur.size)
        values.append(np.concatenate([ref, cur]))
    if len(counts) < 2:
        return float("nan"), 1.0
    n_cur = float(sum(b_obs))
    n_ref = float(sum(c - b for c, b in zip(counts, b_obs)))
    if n_cur < 1 or n_ref < 1:
        return float("nan"), 1.0

    n_patterns = cfg.permutations + 1
    cur_sums = np.zeros((n_patterns, len(counts)), dtype=float)
    rng = np.random.default_rng(seed)
    for j, (c, b, vals) in enumerate(zip(counts, b_obs, values)):
        chosen = np.argpartition(rng.random((n_patterns, c)), b - 1,
                                 axis=1)[:, :b]
        cur_sums[:, j] = vals[chosen].sum(axis=1)
        cur_sums[0, j] = vals[c - b:c].sum()
    total_sums = np.asarray([v.sum() for v in values], dtype=float)
    ref_sums = total_sums - cur_sums
    stat = cur_sums.sum(axis=1) / n_cur - ref_sums.sum(axis=1) / n_ref
    obs = float(stat[0])
    p = float(np.count_nonzero(np.abs(stat) >= abs(obs) - 1e-12)
              / n_patterns)
    return obs, min(1.0, max(0.0, p))


# ---------------------------------------------------------------------------
# detector families
# ---------------------------------------------------------------------------


def _family(ks_p: float, prop_p: float, mmd_p: float,
            alpha: float) -> Dict[str, float]:
    adjusted, flags = benjamini_hochberg_correction(
        [ks_p, prop_p, mmd_p], alpha=alpha)
    return {
        "ks_p": ks_p, "prop_p": prop_p, "mmd_p": mmd_p,
        "ks_adj": adjusted[0], "prop_adj": adjusted[1], "mmd_adj": adjusted[2],
        "ks_flag": int(bool(flags[0])), "prop_flag": int(bool(flags[1])),
        "mmd_flag": int(bool(flags[2])),
        "any_flag": int(any(flags)),
        # Production trigger excludes availability from distribution shift.
        "dist_flag": int(bool(flags[0]) or bool(flags[2])),
    }


def run_current(data: Window, cfg: SimConfig) -> Dict[str, float]:
    ks_stat, ks_p = ks_test(data.lengths_ref, data.lengths_cur)
    prop_stat, prop_p, method = proportion_test(data.valid_ref, data.valid_cur)
    mmd_stat, mmd_p, pairs = paired_cluster_mmd_test(
        data.X, data.Y, data.xl, data.yl,
        n_permutations=cfg.permutations, random_seed=42)
    out = _family(ks_p, prop_p, mmd_p, cfg.alpha)
    out.update({"ks_stat": ks_stat, "prop_stat": prop_stat,
                "mmd_stat": mmd_stat, "pairs": pairs, "method": method})
    return out


def run_paired(data: Window, cfg: SimConfig) -> Dict[str, float]:
    ks_stat, ks_p = paired_mean_length_test(data)
    prop_stat, prop_p = paired_validity_permutation(data, cfg)
    mmd_stat, mmd_p, pairs = allocation_mmd(data, cfg)
    out = _family(ks_p, prop_p, mmd_p, cfg.alpha)
    out.update({"ks_stat": ks_stat, "prop_stat": prop_stat,
                "mmd_stat": mmd_stat, "pairs": pairs, "method": "paired_perm"})
    return out


METHODS = (("current", run_current), ("paired", run_paired))
DETECTORS = ("ks", "prop", "mmd", "any", "dist")


# ---------------------------------------------------------------------------
# reporting helpers
# ---------------------------------------------------------------------------


def wilson(k: int, n: int, alpha: float = ALPHA) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    z = stats.norm.ppf(1.0 - alpha / 2.0)
    phat = k / n
    denom = 1.0 + z * z / n
    centre = (phat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(phat * (1.0 - phat) / n
                         + z * z / (4.0 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def rate_rows(name: str, condition: str, magnitude: str,
              counts: Dict[str, int], trials: int,
              ms: float = 0.0) -> List[Dict[str, object]]:
    rows = []
    for detector in DETECTORS:
        k = counts[detector]
        low, high = wilson(k, trials)
        rows.append({
            "condition": condition, "magnitude": magnitude,
            "method": name, "detector": detector,
            "trials": trials, "rejections": k,
            "rate": k / trials if trials else float("nan"),
            "ci_low": low, "ci_high": high,
            "mean_ms_per_window": round(ms, 3),
        })
    return rows


def banner(text: str) -> None:
    print("\n" + text, flush=True)


# ---------------------------------------------------------------------------
# self-check: fast implementation equals production cluster swap
# ---------------------------------------------------------------------------


def self_check(cfg: SimConfig) -> None:
    small = SimConfig(**{**asdict(cfg), "prompts": 6, "emb_dim": 8,
                         "permutations": 200})
    data = single_window(101, small, Shift())
    _, fast_p, fast_pairs = cluster_swap_mmd(data, small)
    _, prod_p, prod_pairs = paired_cluster_mmd_test(
        data.X, data.Y, data.xl, data.yl, n_permutations=200, random_seed=42)
    if fast_pairs != prod_pairs or not math.isclose(fast_p, prod_p,
                                                    abs_tol=1e-12):
        raise RuntimeError(
            f"fast cluster swap mismatch: {fast_p, fast_pairs} "
            f"vs production {prod_p, prod_pairs}")
    print(f"self-check: fast cluster swap matches production "
          f"(p={prod_p:.4f}, pairs={prod_pairs})", flush=True)


# ---------------------------------------------------------------------------
# benchmarks
# ---------------------------------------------------------------------------


def benchmark_window(seed: int, cfg: SimConfig, shift: Shift,
                     rho: float) -> Tuple[Dict[str, Dict[str, int]],
                                          Dict[str, float]]:
    data = single_window(seed, cfg, shift, rho=rho)
    counts: Dict[str, Dict[str, int]] = {}
    timings: Dict[str, float] = {}
    for name, run in METHODS:
        started = time.perf_counter()
        out = run(data, cfg)
        timings[name] = (time.perf_counter() - started) * 1000.0
        counts[name] = {
            d: int(out[d + "_flag"] if d in ("any", "dist")
                   else out[d + "_p"] < cfg.alpha)
            for d in DETECTORS
        }
    return counts, timings


def null_benchmark(cfg: SimConfig, trials: int,
                   rho: float) -> List[Dict[str, object]]:
    counts = {m: {d: 0 for d in DETECTORS} for m, _ in METHODS}
    elapsed = {m: 0.0 for m, _ in METHODS}
    for i in range(trials):
        cell_counts, cell_ms = benchmark_window(
            10_000 + i, cfg, Shift(), rho)
        for name in counts:
            for d in DETECTORS:
                counts[name][d] += cell_counts[name][d]
            elapsed[name] += cell_ms[name]
    rows: List[Dict[str, object]] = []
    for name, _ in METHODS:
        rows.extend(rate_rows(name, "A_null", f"rho={rho:.2f}",
                              counts[name], trials,
                              elapsed[name] / trials))
    return rows


def power_benchmark(cfg: SimConfig, trials: int) -> List[Dict[str, object]]:
    conditions = {
        "B_length": [(0.3, Shift(length=4.5)),
                     (0.6, Shift(length=9.0)),
                     (1.2, Shift(length=18.0))],
        "C_semantic_mean": [(0.25, Shift(emb_mean=0.25)),
                            (0.5, Shift(emb_mean=0.50)),
                            (1.0, Shift(emb_mean=1.00))],
        "D_semantic_shape": [(1.2, Shift(emb_var=1.20)),
                             (1.5, Shift(emb_var=1.50)),
                             (2.0, Shift(emb_var=2.00))],
        "E_availability": [(0.05, Shift(valid_drop=0.05)),
                           (0.15, Shift(valid_drop=0.15)),
                           (0.25, Shift(valid_drop=0.25))],
        "F_combined": [(0.5, Shift(length=9.0, emb_mean=0.50,
                                   emb_var=1.5, valid_drop=0.15)),
                       (1.0, Shift(length=18.0, emb_mean=1.00,
                                   emb_var=2.0, valid_drop=0.25))],
    }
    rows: List[Dict[str, object]] = []
    for condition, levels in conditions.items():
        for magnitude, shift in levels:
            counts = {m: {d: 0 for d in DETECTORS} for m, _ in METHODS}
            elapsed = {m: 0.0 for m, _ in METHODS}
            for i in range(trials):
                cell_counts, cell_ms = benchmark_window(
                    20_000 + i, cfg, shift, 0.0)
                for name in counts:
                    for d in DETECTORS:
                        counts[name][d] += cell_counts[name][d]
                    elapsed[name] += cell_ms[name]
            for name, _ in METHODS:
                rows.extend(rate_rows(name, condition, str(magnitude),
                                      counts[name], trials,
                                      elapsed[name] / trials))
            print(f"  {condition} magnitude={magnitude} done", flush=True)
    return rows


def alert_benchmark(cfg: SimConfig, sequences: int, rho: float,
                    shift: Shift, shift_start: Optional[int],
                    n_cycles: int = 8) -> Tuple[List[Dict], List[Dict]]:
    """Mirror the production drift gate: dist trigger + any-BH streak >= 2."""
    rows: List[Dict] = []
    delays: List[Dict] = []
    condition = "alert_null" if shift_start is None else "alert_shift"
    for name, run in METHODS:
        alerts = 0
        detected = 0
        delay_sum = 0.0
        for s in range(sequences):
            temporal_cfg = SimConfig(**{**asdict(cfg),
                                        "temporal_emb": rho * cfg.within_emb_std,
                                        "temporal_len": rho * cfg.within_len_std})
            cycles = generate_panel(40_000 + s, temporal_cfg, shift,
                                    n_cycles=n_cycles,
                                    shift_start=(shift_start if shift_start
                                                 is not None else 99))
            streak: List[int] = []
            first_alert: Optional[int] = None
            for t in range(cfg.ref_cycles, n_cycles):
                data = build_window(cycles, list(range(t - cfg.ref_cycles, t)), t)
                out = run(data, cfg)
                streak.append(int(out["any_flag"]))
                if len(streak) > 2:
                    streak.pop(0)
                alert = bool(out["dist_flag"]) and len(streak) == 2 \
                    and all(streak)
                if alert and first_alert is None and shift_start is not None:
                    first_alert = t - shift_start + 1
                elif alert and first_alert is None:
                    first_alert = 0
            if first_alert is not None:
                alerts += 1
                delay_sum += first_alert
                detected += 1
        rows.append(rate_row(name, condition, f"rho={rho:.2f}", alerts,
                             sequences))
        if shift_start is not None:
            low, high = wilson(detected, sequences)
            delays.append({
                "method": name, "rho": rho, "sequences": sequences,
                "detected": detected,
                "rate": detected / sequences if sequences else float("nan"),
                "ci_low": low, "ci_high": high,
                "mean_delay_cycles": (delay_sum / detected
                                      if detected else float("nan")),
            })
    return rows, delays


def rate_row(name: str, condition: str, magnitude: str, k: int,
             n: int) -> Dict[str, object]:
    low, high = wilson(k, n)
    return {"condition": condition, "magnitude": magnitude, "method": name,
            "trials": n, "rejections": k,
            "rate": k / n if n else float("nan"),
            "ci_low": low, "ci_high": high}


# ---------------------------------------------------------------------------
# plots and output
# ---------------------------------------------------------------------------


def write_csv(rows: List[Dict], path: Path) -> None:
    if not rows:
        return
    import csv
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def plot_rates(rows: List[Dict], out: Path, title: str, filename: str,
               detectors: Tuple[str, ...] = ("ks", "prop", "mmd", "any")) \
        -> None:
    if not rows:
        return
    conditions = list(dict.fromkeys(
        (r["condition"], r["magnitude"]) for r in rows))
    x = np.arange(len(conditions))
    width = 0.18
    fig, ax = plt.subplots(figsize=(13, 5))
    colors = {"current": "#d1495b", "paired": "#00798c"}
    offsets = {"current": -width * 2.0, "paired": width * 0.5}
    for method in colors:
        for di, detector in enumerate(detectors):
            ys, lo, hi = [], [], []
            for condition, magnitude in conditions:
                match = [r for r in rows if r["condition"] == condition
                         and r["magnitude"] == magnitude
                         and r["method"] == method
                         and r["detector"] == detector]
                row = match[0] if match else None
                ys.append(row["rate"] if row else float("nan"))
                lo.append(max(0.0, row["rate"] - row["ci_low"])
                          if row else 0.0)
                hi.append(max(0.0, row["ci_high"] - row["rate"])
                          if row else 0.0)
            positions = x + offsets[method] + di * width
            ax.bar(positions, ys, width, label=f"{method}:{detector}",
                   color=colors[method], alpha=0.35 + 0.2 * di,
                   yerr=[lo, hi], capsize=2)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{c}\n{m}" for c, m in conditions], rotation=45,
                       ha="right", fontsize=8)
    ax.axhline(ALPHA, color="black", linestyle="--", linewidth=1,
               label="alpha=0.05")
    ax.set_ylabel("empirical rate")
    ax.set_title(title)
    ax.legend(fontsize=7, ncol=4)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out / filename, dpi=160)
    plt.close(fig)


def simple_bar(rows: List[Dict], out: Path, filename: str, title: str) -> None:
    labels = [f"{r['condition']}\n{r['magnitude']}\n{r['method']}"
              for r in rows]
    rates = [r["rate"] for r in rows]
    los = [max(0.0, r["rate"] - r["ci_low"]) for r in rows]
    his = [max(0.0, r["ci_high"] - r["rate"]) for r in rows]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.bar(labels, rates, yerr=[los, his], capsize=3,
           color=["#d1495b" if r["method"] == "current" else "#00798c"
                  for r in rows])
    ax.set_ylabel("rate")
    ax.set_title(title)
    ax.tick_params(axis="x", labelsize=7)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    fig.savefig(out / filename, dpi=160)
    plt.close(fig)


def git_sha() -> Optional[str]:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              check=True, capture_output=True, text=True
                              ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="small trial counts for a smoke run")
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--power-trials", type=int, default=500)
    parser.add_argument("--sequences", type=int, default=300)
    parser.add_argument("--permutations", type=int, default=999)
    parser.add_argument("--out", default=str(ROOT / "results" /
                                            "statistical_audit"))
    args = parser.parse_args()
    if args.quick:
        args.trials = 200
        args.power_trials = 60
        args.sequences = 40

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = SimConfig(permutations=args.permutations)
    started = time.time()
    banner("self-check")
    self_check(cfg)

    banner(f"null calibration: {args.trials} trials per rho x method")
    null_rows: List[Dict] = []
    for rho in (0.0, 0.30, 0.60):
        null_rows.extend(null_benchmark(cfg, args.trials, rho))

    banner(f"power curves: {args.power_trials} trials per cell")
    power_rows = power_benchmark(cfg, args.power_trials)

    banner(f"alert persistence: {args.sequences} sequences")
    alert_rows: List[Dict] = []
    delay_rows: List[Dict] = []
    for rho in (0.0, 0.30):
        null_alerts, _ = alert_benchmark(cfg, args.sequences, rho, Shift(), None)
        alert_rows.extend(null_alerts)
        shift_alerts, delays = alert_benchmark(
            cfg, args.sequences, rho,
            Shift(length=9.0, emb_mean=0.50, emb_var=1.5, valid_drop=0.15),
            shift_start=3)
        alert_rows.extend(shift_alerts)
        delay_rows.extend(delays)

    write_csv(null_rows, out / "null_type_i.csv")
    write_csv(power_rows, out / "power.csv")
    write_csv(alert_rows, out / "alert_rates.csv")
    write_csv(delay_rows, out / "detection_delay.csv")

    plot_rates([r for r in null_rows], out,
               "Null type-I error by temporal correlation", "null_type_i.png")
    plot_rates([r for r in null_rows if r["detector"] == "any"], out,
               "Any-detector false-positive rate with BH", "null_any.png",
               detectors=("any",))
    plot_rates(power_rows, out, "Power by shift condition", "power_curves.png")
    simple_bar([r for r in alert_rows if r["condition"] == "alert_null"],
               out, "alert_null.png", "Two-cycle alert false alarms")
    simple_bar([r for r in alert_rows if r["condition"] == "alert_shift"],
               out, "alert_power.png", "Two-cycle alert detection")

    summary = {
        "commit": git_sha(),
        "config": asdict(cfg),
        "trials": {"null": args.trials, "power": args.power_trials,
                   "alert_sequences": args.sequences},
        "elapsed_seconds": round(time.time() - started, 1),
        "outputs": sorted(p.name for p in out.iterdir()),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    banner(f"done in {summary['elapsed_seconds']}s; outputs in {out}")


if __name__ == "__main__":
    main()
