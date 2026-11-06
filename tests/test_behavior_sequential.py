"""Tests for behavioral features, sequential alert policy, and new detectors."""

from behavior_features import extract_behavior_features, is_valid_json, refusal_rate
import pytest

from drift_tests import centroid_distance
from sequential_testing import (cumulative_spent, consecutive_flagged_history,
                                consecutive_streak, spent_alpha)


class TestBehaviorFeatures:
    def test_refusal_detected(self):
        features = extract_behavior_features("I cannot help with that request.")
        assert features['is_refusal'] == 1

    def test_normal_answer_not_a_refusal(self):
        features = extract_behavior_features("The capital of France is Paris.")
        assert features['is_refusal'] == 0

    def test_empty_response(self):
        features = extract_behavior_features("   ")
        assert features['is_empty'] == 1
        assert features['token_count'] == 0
        assert features['repetition_ratio'] == 0.0

    def test_none_is_handled_like_empty(self):
        features = extract_behavior_features(None)
        assert features['is_empty'] == 1

    def test_code_block_detected(self):
        features = extract_behavior_features("Here you go:\n```python\nprint(1)\n```")
        assert features['has_code_block'] == 1

    def test_repetition_ratio_bounds(self):
        features = extract_behavior_features("the the the the")
        assert features['repetition_ratio'] == 0.75
        features = extract_behavior_features("one two three four five")
        assert features['repetition_ratio'] == 0.0

    def test_json_validation(self):
        assert is_valid_json('{"a": 1}') is True
        assert is_valid_json('not json') is False
        assert is_valid_json('') is False

    def test_refusal_rate(self):
        assert refusal_rate([1, 0, 0, 1]) == 0.5
        assert refusal_rate([]) == 0.0


class TestSequentialPolicy:
    def test_persistence_requires_full_streak(self):
        assert consecutive_flagged_history([1, 1], 2) is True
        assert consecutive_flagged_history([0, 1, 1], 2) is True
        assert consecutive_flagged_history([1, 0, 1], 2) is False
        assert consecutive_flagged_history([1], 2) is False

    def test_persistence_of_one_reproduces_old_behavior(self):
        assert consecutive_flagged_history([0, 0, 1], 1) is True
        assert consecutive_flagged_history([0, 0], 1) is False

    def test_streak_counts_trailing_rejections(self):
        assert consecutive_streak([1, 1, 0, 1, 1, 1]) == 3
        assert consecutive_streak([0, 0]) == 0
        assert consecutive_streak([]) == 0

    def test_spending_sums_exactly_to_alpha(self):
        alpha, horizon = 0.05, 30
        total = sum(spent_alpha(t, horizon, alpha) for t in range(1, horizon + 1))
        assert abs(total - alpha) < 1e-12

    def test_spending_is_conservative_early(self):
        alpha, horizon = 0.05, 30
        assert spent_alpha(1, horizon, alpha) < spent_alpha(15, horizon, alpha)
        assert spent_alpha(1, horizon, alpha) < alpha / horizon < spent_alpha(horizon, horizon, alpha)

    def test_spending_capped_outside_range(self):
        assert spent_alpha(0, 30, 0.05) == 0.05
        assert spent_alpha(5, 0, 0.05) == 0.05

    def test_holm_is_familywise_and_monotone(self):
        from drift_tests import holm_correction
        adjusted, flags = holm_correction([0.01, 0.04, 0.5], alpha=0.05)
        # Holm: smallest times n, then increasing multipliers, monotone enforced.
        assert adjusted[0] == pytest.approx(0.03)
        assert adjusted[1] == pytest.approx(0.08)
        assert flags == [True, False, False]

    def test_holm_rejects_less_often_than_bh(self):
        from drift_tests import benjamini_hochberg_correction, holm_correction
        pvalues = [0.03, 0.03, 0.04]
        _, bh_flags = benjamini_hochberg_correction(pvalues, alpha=0.05)
        _, holm_flags = holm_correction(pvalues, alpha=0.05)
        assert any(bh_flags)
        assert not all(holm_flags)


    def test_cumulative_never_exceeds_alpha(self):
        assert cumulative_spent(100, 30, 0.05) == 0.05
        assert cumulative_spent(3, 30, 0.05) < 0.05


class TestCentroidBaseline:
    def test_identical_distributions_have_zero_distance(self):
        points = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
        assert centroid_distance(points, points) == 0.0

    def test_mean_shift_is_detected(self):
        ref = [[0.0, 0.0], [0.5, 0.0], [0.0, 0.5], [0.5, 0.5]]
        # Shift orthogonal to the mean direction, so the centroid clearly moves.
        cur = [[1.0, -0.5], [1.5, -0.5], [1.0, 0.0], [1.5, 0.0]]
        assert centroid_distance(ref, cur) > 0.3

    def test_scaled_spread_keeps_centroid_fixed(self):
        # Same mean, wider spread: the mean-based baseline is blind to this,
        # which is exactly why MMD is needed.
        ref = [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0]]
        cur = [[0.1, 0.0], [0.0, 0.1], [-0.1, 0.0], [0.0, -0.1]]
        assert centroid_distance(ref, cur) < 1e-9

    def test_empty_samples(self):
        assert centroid_distance([], [[1.0]]) == 0.0
        assert centroid_distance([[1.0]], []) == 0.0

    def test_zero_vector_edge(self):
        ref = [[0.0, 0.0], [0.0, 0.0]]
        cur = [[1.0, 0.0], [1.0, 0.0]]
        assert centroid_distance(ref, cur) == 1.0
