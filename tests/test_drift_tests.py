import numpy as np
from drift_tests import (
    ks_test,
    two_proportion_z_test,
    mmd_test,
    benjamini_hochberg_correction,
    category_mmd_tests,
    count_unique_embeddings,
    unique_embeddings
)

class TestKSTest:
    def test_ks_identical_distributions(self):
        """KS test should give high p-value for identical distributions."""
        np.random.seed(42)
        ref = np.random.normal(0, 1, 100).tolist()
        curr = np.random.normal(0, 1, 100).tolist()
        stat, pval = ks_test(ref, curr)
        assert not np.isnan(pval)
        assert pval > 0.05  # Not significant for identical distributions
    
    def test_ks_shifted_distributions(self):
        """KS test should give low p-value for shifted distributions."""
        np.random.seed(42)
        ref = np.random.normal(0, 1, 100).tolist()
        curr = np.random.normal(1, 1, 100).tolist()  # Shifted mean
        stat, pval = ks_test(ref, curr)
        assert not np.isnan(pval)
        assert pval < 0.05  # Should detect shift
    
    def test_ks_insufficient_samples(self):
        """KS test should handle insufficient samples gracefully."""
        ref = [1, 2, 3]
        curr = []
        stat, pval = ks_test(ref, curr)
        # Should not crash, should return non-significant
        assert pval >= 0.05 or np.isnan(stat)

class TestTwoProportionZTest:
    def test_proportion_identical(self):
        """Two-proportion test should give high p-value for identical proportions."""
        ref = [1, 1, 1, 0, 0, 1, 1, 0, 1, 0]  # 6/10 valid
        curr = [1, 1, 0, 0, 1, 1, 1, 0, 1, 0]  # 6/10 valid
        stat, pval = two_proportion_z_test(ref, curr)
        if not np.isnan(pval):
            assert pval > 0.05
    
    def test_proportion_shifted(self):
        """Two-proportion test should detect shifted proportions."""
        ref = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]  # 100% valid
        curr = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]  # 0% valid
        stat, pval = two_proportion_z_test(ref, curr)
        if not np.isnan(pval):
            assert pval < 0.05  # Should detect large shift
    
    def test_proportion_insufficient_counts(self):
        """Two-proportion test should handle small samples."""
        ref = [1, 0]  # n < 5
        curr = [1, 1]
        stat, pval = two_proportion_z_test(ref, curr)
        # Should return conservative result for small samples
        assert pval >= 0.05 or np.isnan(pval)

class TestBenjaminiHochberg:
    def test_bh_identical_pvalues(self):
        """BH correction should work with equal p-values."""
        pvals = [0.01, 0.01, 0.01]
        adj, sig = benjamini_hochberg_correction(pvals, alpha=0.05)
        assert len(adj) == 3
        assert len(sig) == 3
        # All should be significant with BH for small p-values
        assert all(sig)
    
    def test_bh_mixed_pvalues(self):
        """BH correction should handle mixed significant/non-significant."""
        pvals = [0.001, 0.04, 0.2, 0.8]
        adj, sig = benjamini_hochberg_correction(pvals, alpha=0.05)
        assert len(adj) == 4
        assert sig[0]  # 0.001
        assert not sig[1]  # 0.04 (with correct BH adjustment)
        assert not sig[2]  # 0.2
        assert not sig[3]  # 0.8
    
    def test_bh_known_example(self):
        """BH correction against a hand-computable known example."""
        pvals = [0.001, 0.01, 0.02, 0.03, 0.5]
        adj, sig = benjamini_hochberg_correction(pvals, alpha=0.05)
        expected_adj = [0.005, 0.025, 0.0333333333, 0.0375, 0.5]
        assert len(adj) == 5
        for got, want in zip(adj, expected_adj):
            assert abs(float(got) - want) < 1e-9
        assert all(bool(s) for s in sig[:4])
        assert not bool(sig[4])
    
    def test_bh_empty(self):
        """BH correction should handle empty list."""
        adj, sig = benjamini_hochberg_correction([])
        assert adj == []
        assert sig == []
    
    def test_bh_with_nans(self):
        """BH correction should handle NaN values."""
        pvals = [0.01, float('nan'), 0.5]
        adj, sig = benjamini_hochberg_correction(pvals)
        assert len(adj) == 3
        assert len(sig) == 3

class TestMMD:
    def test_mmd_identical_distributions(self):
        """MMD should give high p-value for identical distributions."""
        np.random.seed(42)
        X = np.random.normal(0, 1, (20, 10)).tolist()  # 20 samples, 10 dims
        Y = np.random.normal(0, 1, (20, 10)).tolist()
        stat, pval = mmd_test(X, Y, n_permutations=100, random_seed=42)
        assert not np.isnan(pval)
        assert pval > 0.05  # Should not detect difference
    
    def test_mmd_shifted_distributions(self):
        """MMD should give low p-value for shifted distributions."""
        np.random.seed(42)
        X = np.random.normal(0, 1, (30, 5)).tolist()
        Y = np.random.normal(2, 1, (30, 5)).tolist()  # Significant shift
        stat, pval = mmd_test(X, Y, n_permutations=200, random_seed=42)
        assert not np.isnan(pval)
        assert pval < 0.05  # Should detect shift
    
    def test_mmd_reproducibility(self):
        """MMD should be reproducible with fixed seed."""
        np.random.seed(42)
        X = np.random.normal(0, 1, (15, 4)).tolist()
        Y = np.random.normal(0, 1, (15, 4)).tolist()
        stat1, pval1 = mmd_test(X, Y, n_permutations=50, random_seed=123)
        stat2, pval2 = mmd_test(X, Y, n_permutations=50, random_seed=123)
        assert abs(stat1 - stat2) < 1e-10
        assert abs(pval1 - pval2) < 1e-10
    
    def test_mmd_insufficient_samples(self):
        """MMD should handle insufficient samples."""
        X = [[1.0, 2.0]]
        Y = []
        stat, pval = mmd_test(X, Y)
        assert pval >= 0.05

    def test_mmd_median_heuristic_high_dimensional(self):
        """MMD with median heuristic should properly detect shifts in 384 dimensions."""
        np.random.seed(42)
        X = np.random.normal(0, 1, (30, 384)).tolist()
        # Null test: same distribution
        Y_null = np.random.normal(0, 1, (30, 384)).tolist()
        stat_null, pval_null = mmd_test(X, Y_null, n_permutations=100, random_seed=42)
        assert not np.isnan(pval_null)
        assert pval_null > 0.01  # Not rejected under null

        # Shifted test: mean shifted by 1.0 along all dimensions
        Y_shifted = (np.random.normal(0, 1, (30, 384)) + 0.5).tolist()
        stat_shifted, pval_shifted = mmd_test(X, Y_shifted, n_permutations=100, random_seed=42)
        assert not np.isnan(pval_shifted)
        assert pval_shifted < 0.05  # Detected under shift

class TestUniqueCounts:
    """Effective (unique) sample counts expose pseudoreplication in results.
    A deterministic adapter repeats the same answers, so nominal run counts
    must not be mistaken for independent observations."""

    def test_all_distinct(self):
        vectors = [[1.0, 0.0], [0.0, 1.0], [2.0, 2.0]]
        assert count_unique_embeddings(vectors) == 3

    def test_duplicates_collapsed(self):
        vectors = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
        assert count_unique_embeddings(vectors) == 1

    def test_pseudoreplicated_category_skipped_with_counts(self):
        # Eight nominal runs per side, but only two distinct answers on the
        # reference side: the gate applies to distinct observations, so no
        # p value is produced, and the counts still come through.
        base = [[1.0, 0.0], [0.0, 1.0], [2.0, 2.0], [3.0, 3.0],
                [4.0, 4.0], [5.0, 5.0], [6.0, 6.0], [7.0, 7.0]]
        pseudorep = base[:2] * 4  # eight runs, two distinct answers
        distinct = [v[:] for v in base]  # eight runs, eight distinct answers
        results = category_mmd_tests(
            {'cat_a': pseudorep}, {'cat_a': distinct},
            min_samples_per_category=8, n_permutations=50, random_seed=42)
        cat = results['cat_a']
        assert cat['mmd_statistic'] is None and cat['pvalue'] is None
        assert 'insufficient distinct responses' in cat['reason']
        assert cat['n_reference'] == 8 and cat['n_current'] == 8
        assert cat['unique_reference'] == 2 and cat['unique_current'] == 8
        assert cat['pseudoreplicated'] == 1

    def test_duplicates_within_gate_still_tested_and_flagged(self):
        # Ten runs, eight of them distinct on both sides: the effective gate
        # passes, the test runs, and the pseudoreplicated flag records that
        # the nominal count overstates the evidence.
        distinct = [[float(i), float(i)] for i in range(10)]
        with_dupes = distinct[:8] + [distinct[0], distinct[1]]
        results = category_mmd_tests(
            {'cat_a': [v[:] for v in with_dupes]},
            {'cat_a': [v[:] for v in with_dupes]},
            min_samples_per_category=8, n_permutations=50, random_seed=42)
        cat = results['cat_a']
        assert cat['mmd_statistic'] is not None and cat['pvalue'] is not None
        assert cat['unique_reference'] == 8 and cat['unique_current'] == 8
        assert cat['pseudoreplicated'] == 1

    def test_fully_distinct_not_flagged(self):
        vectors = [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0],
                   [4.0, 0.0], [5.0, 0.0], [6.0, 0.0], [7.0, 0.0]]
        results = category_mmd_tests(
            {'cat_a': [v[:] for v in vectors]}, {'cat_a': [v[:] for v in vectors]},
            min_samples_per_category=8, n_permutations=50, random_seed=42)
        cat = results['cat_a']
        assert cat['unique_reference'] == 8
        assert cat['unique_current'] == 8
        assert cat['pseudoreplicated'] == 0

    def test_skipped_category_still_reports_counts(self):
        results = category_mmd_tests(
            {'thin': [[1.0, 0.0], [1.0, 0.0]]},
            {'thin': [[1.0, 0.0]]},
            min_samples_per_category=8)
        cat = results['thin']
        assert cat['mmd_statistic'] is None
        assert cat['n_reference'] == 2 and cat['n_current'] == 1
        assert cat['unique_reference'] == 1
        assert cat['pseudoreplicated'] == 1


class TestDeduplication:
    """MMD treats duplicate vectors as one point, not repeated samples.
    Ten copies of one answer are one observation; otherwise the permutation
    null would grant a comparison of two single points the confidence of a
    ten versus ten test."""

    def test_duplicates_equivalent_to_manual_dedup(self):
        np.random.seed(7)
        base_x = np.random.normal(0, 1, (6, 8)).tolist()
        base_y = np.random.normal(2, 1, (6, 8)).tolist()
        x_duped = base_x + base_x[:2] + base_x[:2]
        stat_dup, p_dup = mmd_test(x_duped, base_y, n_permutations=200,
                                   random_seed=3)
        stat_ref, p_ref = mmd_test(base_x, base_y, n_permutations=200,
                                   random_seed=3)
        assert abs(stat_dup - stat_ref) < 1e-12
        assert abs(p_dup - p_ref) < 1e-12

    def test_two_single_point_masses_not_significant(self):
        # Naively, ten copies of one vector versus ten copies of another far
        # vector produce a tiny p value; deduplicated it is one observation
        # against one, which cannot support distributional inference.
        x = [[0.0, 0.0]] * 10
        y = [[10.0, 10.0]] * 10
        stat, p = mmd_test(x, y, n_permutations=500, random_seed=42)
        assert (stat, p) == (0.0, 1.0)

    def test_duplicated_multi_point_windows_no_spurious_rejection(self):
        # Two distinct answers per side, five copies of each: the collapse is
        # valid, but at two effective observations per side the permutation
        # has almost no power. The honest outcome is a moderate p value, in
        # contrast to the tiny p value the duplicates would have manufactured.
        x = [[0.0, 0.0], [1.0, 0.0]] * 5
        y = [[100.0, 0.0], [101.0, 0.0]] * 5
        stat, p = mmd_test(x, y, n_permutations=200, random_seed=42)
        assert stat > 0.0
        assert 0.0 < p <= 1.0
        assert p > 1.0 / 201  # no fabricated near zero p from mass repeats

    def test_unique_embeddings_helper_preserves_order(self):
        vectors = [[2.0, 2.0], [1.0, 1.0], [2.0, 2.0]]
        assert unique_embeddings(vectors) == [[2.0, 2.0], [1.0, 1.0]]


class TestPipelineIntegration:
    def test_fake_adapter_pipeline(self):
        """Test complete pipeline with fake adapter."""
        from monitor import LLMDriftMonitor
        import tempfile
        import os
        import yaml
        
        # Keep this storage smoke test independent of a local Ollama installation.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            with open("config/config.yaml", "r") as config_file:
                config = yaml.safe_load(config_file)
            config["target"] = {"type": "fake", "model_id": "fake-test", "temperature": 0.0}
            config["data"]["db_path"] = db_path
            config_path = os.path.join(tmpdir, "test-config.yaml")
            with open(config_path, "w") as config_file:
                yaml.safe_dump(config, config_file)

            monitor = LLMDriftMonitor(config_path=config_path)
            monitor.embedder.extract_embedding = lambda text: [float(len(text)), 1.0]
            run_ids = monitor.run_canary_probes(num_prompts=5)
            assert len(run_ids) == 5
            assert monitor.storage.count_successful_runs() == 5
