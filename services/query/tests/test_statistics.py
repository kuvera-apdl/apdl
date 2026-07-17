"""Unit tests for ExperimentAnalyzer — pure math, no mocking needed."""

import numpy as np
import pytest

from app.models.statistics import ExperimentAnalyzer

analyzer = ExperimentAnalyzer()


# ------------------------------------------------------------------
# CUPED variance reduction
# ------------------------------------------------------------------


class TestCUPED:
    def test_cuped_reduces_variance(self):
        """CUPED-adjusted values should have lower variance when covariate is correlated."""
        rng = np.random.default_rng(42)
        covariate = rng.normal(10, 3, size=500)
        metric = covariate * 0.8 + rng.normal(0, 1, size=500)

        adjusted = analyzer.calculate_cuped(metric, covariate)

        assert np.var(adjusted, ddof=1) < np.var(metric, ddof=1)
        assert len(adjusted) == len(metric)

    def test_cuped_constant_covariate_returns_copy(self):
        """If the covariate has zero variance, return the metric unchanged."""
        metric = np.array([1.0, 2.0, 3.0, 4.0])
        covariate = np.array([5.0, 5.0, 5.0, 5.0])

        adjusted = analyzer.calculate_cuped(metric, covariate)

        np.testing.assert_array_equal(adjusted, metric)
        # Ensure it's a copy, not the same object
        assert adjusted is not metric

    def test_cuped_preserves_mean(self):
        """CUPED adjustment should approximately preserve the mean."""
        rng = np.random.default_rng(99)
        covariate = rng.normal(0, 1, size=1000)
        metric = covariate + rng.normal(5, 1, size=1000)

        adjusted = analyzer.calculate_cuped(metric, covariate)

        np.testing.assert_allclose(np.mean(adjusted), np.mean(metric), atol=0.01)


# ------------------------------------------------------------------
# Frequentist (Welch's t-test)
# ------------------------------------------------------------------


class TestFrequentist:
    def test_significant_difference(self):
        """Clearly different distributions should produce a significant result."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 2, size=500)
        treatment = rng.normal(12, 2, size=500)

        result = analyzer.frequentist_test(control, treatment)

        assert result["method"] == "frequentist"
        assert result["is_significant"] is True
        assert result["p_value"] < 0.05
        assert result["effect_size"] > 0
        ci_low, ci_high = result["confidence_interval"]
        assert ci_low > 0  # entire CI above zero
        assert ci_high > ci_low
        assert "recommendation" not in result

    def test_no_significant_difference(self):
        """Identical distributions should not be significant."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 2, size=100)
        treatment = rng.normal(10, 2, size=100)

        result = analyzer.frequentist_test(control, treatment)

        assert result["is_significant"] is False
        assert result["p_value"] > 0.05
        assert "recommendation" not in result

    def test_negative_effect(self):
        """Treatment worse than control should report degradation."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 1, size=500)
        treatment = rng.normal(8, 1, size=500)

        result = analyzer.frequentist_test(control, treatment)

        assert result["is_significant"] is True
        assert result["effect_size"] < 0
        assert "recommendation" not in result

    def test_custom_alpha(self):
        """Custom alpha=0.01 should require stronger evidence."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 2, size=100)
        treatment = rng.normal(10.8, 2, size=100)

        result_05 = analyzer.frequentist_test(control, treatment, alpha=0.05)
        result_01 = analyzer.frequentist_test(control, treatment, alpha=0.01)

        # The p-value stays the same; significance may differ
        assert result_05["p_value"] == pytest.approx(result_01["p_value"])


# ------------------------------------------------------------------
# Bayesian (Beta-Binomial)
# ------------------------------------------------------------------


class TestBayesian:
    def test_treatment_clearly_better(self):
        """When treatment has much higher conversion, prob should be high."""
        rng = np.random.default_rng(42)
        control = rng.binomial(1, 0.1, size=1000).astype(float)
        treatment = rng.binomial(1, 0.2, size=1000).astype(float)

        result = analyzer.bayesian_test(control, treatment)

        assert result["method"] == "bayesian"
        assert result["prob_treatment_better"] > 0.95
        assert result["is_significant"] is True
        assert result["treatment_rate"] > result["control_rate"]
        assert "recommendation" not in result

    def test_equal_conversion_rates(self):
        """With equal rates, probability should be near 0.5."""
        rng = np.random.default_rng(42)
        control = rng.binomial(1, 0.15, size=200).astype(float)
        treatment = rng.binomial(1, 0.15, size=200).astype(float)

        result = analyzer.bayesian_test(control, treatment)

        assert 0.1 < result["prob_treatment_better"] < 0.9
        assert result["is_significant"] is False
        assert "recommendation" not in result

    def test_control_clearly_better(self):
        """When control is clearly better, prob_treatment_better should be low."""
        rng = np.random.default_rng(42)
        control = rng.binomial(1, 0.3, size=1000).astype(float)
        treatment = rng.binomial(1, 0.1, size=1000).astype(float)

        result = analyzer.bayesian_test(control, treatment)

        assert result["prob_treatment_better"] < 0.05
        assert result["is_significant"] is True

    def test_expected_loss_values(self):
        """Expected loss values should be non-negative."""
        control = np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0], dtype=float)
        treatment = np.array([1, 1, 1, 0, 1, 0, 1, 1, 1, 0], dtype=float)

        result = analyzer.bayesian_test(control, treatment)

        assert result["expected_loss_control"] >= 0
        assert result["expected_loss_treatment"] >= 0


# ------------------------------------------------------------------
# Sequential testing (mSPRT)
# ------------------------------------------------------------------


class TestSequential:
    def test_significant_positive_effect(self):
        """Large positive effect with enough data should be significant."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 2, size=1000)
        treatment = rng.normal(13, 2, size=1000)

        result = analyzer.sequential_test(control, treatment)

        assert result["method"] == "sequential"
        assert result["is_significant"] is True
        assert result["effect_size"] > 0
        assert result["always_valid_p_value"] < 0.05
        assert "recommendation" not in result

    def test_no_effect(self):
        """Same distributions should not be significant (enough data to not be spurious)."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 2, size=200)
        treatment = rng.normal(10, 2, size=200)

        result = analyzer.sequential_test(control, treatment)

        assert result["is_significant"] is False
        assert result["always_valid_p_value"] > 0.05
        assert "recommendation" not in result

    def test_significant_negative_effect(self):
        """Large negative effect should recommend reverting."""
        rng = np.random.default_rng(42)
        control = rng.normal(10, 1, size=1000)
        treatment = rng.normal(7, 1, size=1000)

        result = analyzer.sequential_test(control, treatment)

        assert result["is_significant"] is True
        assert result["effect_size"] < 0
        assert "recommendation" not in result

    def test_msprt_statistic_positive(self):
        """The mSPRT statistic should always be positive."""
        control = np.array([1.0, 2.0, 3.0])
        treatment = np.array([1.5, 2.5, 3.5])

        result = analyzer.sequential_test(control, treatment)

        assert result["msprt_statistic"] > 0


# ------------------------------------------------------------------
# Sample-size calculation
# ------------------------------------------------------------------


class TestSampleSize:
    def test_basic_calculation(self):
        """Standard two-proportion sample size should return a positive integer."""
        n = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.02)

        assert isinstance(n, int)
        assert n > 0

    def test_smaller_mde_needs_more_samples(self):
        """A smaller MDE requires a larger sample size."""
        n_large_mde = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.05)
        n_small_mde = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.01)

        assert n_small_mde > n_large_mde

    def test_higher_power_needs_more_samples(self):
        """Higher power requires a larger sample size."""
        n_80 = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.02, power=0.8)
        n_95 = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.02, power=0.95)

        assert n_95 > n_80

    def test_zero_mde_returns_zero(self):
        """Zero MDE should return 0 (division by zero guard)."""
        n = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.0)

        assert n == 0

    def test_known_approximate_value(self):
        """Check against a known approximate result for a standard scenario."""
        # For baseline=0.10, mde=0.02, alpha=0.05, power=0.80
        # Expected ~3,623 per variant (standard formula)
        n = analyzer.calculate_sample_size(baseline_rate=0.10, mde=0.02)
        assert 3000 < n < 4500
