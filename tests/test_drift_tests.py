import numpy as np
from drift_tests import (
    ks_test, 
    two_proportion_z_test, 
    mmd_test, 
    benjamini_hochberg_correction
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
            with open("config.yaml", "r") as config_file:
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
