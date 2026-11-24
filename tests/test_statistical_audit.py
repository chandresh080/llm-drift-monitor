"""Regression tests for the sampling-design audit and prompt-level tests.

The audit helpers and the production functions they motivated are exercised
on the deployed unbalanced 3:1 prompt-cluster shape, including null
calibration and balanced/unequal branches.
"""

import math

from drift_tests import (
    allocation_preserving_mmd_test,
    paired_cluster_mmd_test,
    paired_mean_length_test as production_paired_mean_length_test,
    paired_validity_permutation_test,
)
from experiments.sampling_design_calibration import (
    Shift,
    SimConfig,
    allocation_mmd,
    cluster_swap_mmd,
    paired_mean_length_test,
    paired_validity_permutation,
    single_window,
)


def small_config(**overrides):
    base = SimConfig(prompts=12, emb_dim=8, permutations=199)
    return SimConfig(**{**base.__dict__, **overrides})


class TestAuditHelpers:
    def test_fast_cluster_swap_matches_production_unbalanced(self):
        cfg = small_config(prompts=6)
        data = single_window(101, cfg, Shift())
        _, fast_p, fast_pairs = cluster_swap_mmd(data, cfg)
        _, prod_p, prod_pairs = paired_cluster_mmd_test(
            data.X, data.Y, data.xl, data.yl,
            n_permutations=cfg.permutations, random_seed=42)
        assert fast_pairs == prod_pairs > 0
        assert math.isclose(fast_p, prod_p, abs_tol=1e-12)

    def test_allocation_mmd_detects_shift_with_unequal_clusters(self):
        cfg = small_config()
        data = single_window(202, cfg, Shift(emb_mean=5.0))
        stat, p, pairs = allocation_mmd(data, cfg)
        assert pairs >= 10
        assert p <= 0.05

    def test_allocation_mmd_null_is_not_anti_conservative(self):
        cfg = small_config()
        pvalues = [allocation_mmd(single_window(300 + i, cfg, Shift()),
                                  cfg)[1] for i in range(100)]
        assert sum(p < 0.05 for p in pvalues) <= 15

    def test_paired_length_test_calibration_and_power(self):
        cfg = small_config()
        null_p = [paired_mean_length_test(
            single_window(400 + i, cfg, Shift()))[1] for i in range(100)]
        assert sum(p < 0.05 for p in null_p) <= 15
        _, shifted_p = paired_mean_length_test(
            single_window(500, cfg, Shift(length=9.0)))
        assert shifted_p < 0.05

    def test_paired_validity_permutation_calibration_and_power(self):
        cfg = small_config()
        null_p = [paired_validity_permutation(
            single_window(600 + i, cfg, Shift()), cfg)[1]
            for i in range(100)]
        assert sum(p < 0.05 for p in null_p) <= 15
        _, shifted_p = paired_validity_permutation(
            single_window(700, cfg, Shift(valid_drop=0.25)), cfg)
        assert shifted_p < 0.05


class TestProductionPromptLevelTests:
    """The functions wired into the detector, on balanced and unequal data."""

    def test_allocation_mmd_balanced_exhaustive(self):
        cfg = small_config(prompts=7, ref_cycles=1)
        data = single_window(800, cfg, Shift(emb_mean=10.0))
        stat, p, pairs, method = allocation_preserving_mmd_test(
            data.X, data.Y, data.xl, data.yl,
            random_seed=42, n_permutations=cfg.permutations)
        assert method == 'exhaustive'
        assert pairs > 0
        assert p <= 0.05

    def test_allocation_mmd_unequal_sampled(self):
        cfg = small_config(prompts=8)
        data = single_window(810, cfg, Shift(emb_var=2.0))
        stat, p, pairs, method = allocation_preserving_mmd_test(
            data.X, data.Y, data.xl, data.yl,
            random_seed=42, n_permutations=cfg.permutations)
        assert method == 'sampled'
        assert pairs > 0
        assert 0.0 < p <= 1.0

    def test_allocation_mmd_null_calibration(self):
        cfg = small_config(prompts=8)
        pvalues = []
        for i in range(60):
            data = single_window(820 + i, cfg, Shift())
            pvalues.append(allocation_preserving_mmd_test(
                data.X, data.Y, data.xl, data.yl,
                random_seed=42, n_permutations=cfg.permutations)[1])
        assert sum(p < 0.05 for p in pvalues) <= 12

    def test_allocation_mmd_no_shared_prompts(self):
        stat, p, pairs, method = allocation_preserving_mmd_test(
            [[1.0, 2.0], [3.0, 4.0]], [[9.0, 9.0], [8.0, 8.0]],
            ['a', 'b'], ['c', 'd'])
        assert math.isnan(stat) and p == 1.0 and pairs == 0
        assert method == 'unavailable'

    def test_paired_length_null_calibration_and_shift(self):
        cfg = small_config()
        null_p = []
        for i in range(60):
            data = single_window(830 + i, cfg, Shift())
            null_p.append(production_paired_mean_length_test(
                data.lengths_ref_by_prompt,
                data.lengths_cur_by_prompt)[1])
        assert sum(p < 0.05 for p in null_p) <= 12
        data = single_window(840, cfg, Shift(length=9.0))
        _, p = production_paired_mean_length_test(
            data.lengths_ref_by_prompt, data.lengths_cur_by_prompt)
        assert p < 0.05

    def test_paired_validity_null_calibration_and_shift(self):
        cfg = small_config()
        null_p = []
        for i in range(60):
            data = single_window(850 + i, cfg, Shift())
            null_p.append(paired_validity_permutation_test(
                data.valid_ref_by_prompt, data.valid_cur_by_prompt,
                random_seed=42, n_permutations=cfg.permutations)[1])
        assert sum(p < 0.05 for p in null_p) <= 12
        data = single_window(861, cfg, Shift(valid_drop=0.25))
        _, p = paired_validity_permutation_test(
            data.valid_ref_by_prompt, data.valid_cur_by_prompt,
            random_seed=42, n_permutations=cfg.permutations)
        assert p < 0.05
