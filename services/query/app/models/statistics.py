"""Statistical analysis engines for experiment evaluation."""

from __future__ import annotations

import math

import numpy as np
from scipy import stats


class ExperimentAnalyzer:
    """Provides multiple statistical testing methodologies for A/B experiments.

    Supports:
    - CUPED variance reduction
    - Frequentist (Welch's t-test)
    - Bayesian (Beta-Binomial for conversion rates)
    - Sequential testing (mixture sequential probability ratio test)
    - Sample-size calculation
    """

    # ------------------------------------------------------------------
    # CUPED variance reduction
    # ------------------------------------------------------------------

    def calculate_cuped(
        self,
        metric_values: np.ndarray,
        covariate_values: np.ndarray,
    ) -> np.ndarray:
        """Apply CUPED (Controlled-experiment Using Pre-Experiment Data).

        CUPED reduces variance by adjusting the metric with a correlated
        pre-experiment covariate:

            adjusted = metric - theta * (covariate - mean(covariate))

        where theta = cov(metric, covariate) / var(covariate).

        If the covariate has zero variance (constant), the metric is
        returned unchanged.
        """
        cov_var = np.var(covariate_values, ddof=1)
        if cov_var == 0:
            return metric_values.copy()

        cov_xy = np.cov(metric_values, covariate_values, ddof=1)[0, 1]
        theta = cov_xy / cov_var
        adjusted = metric_values - theta * (covariate_values - np.mean(covariate_values))
        return adjusted

    # ------------------------------------------------------------------
    # Frequentist (Welch's t-test)
    # ------------------------------------------------------------------

    def frequentist_test(
        self,
        control: np.ndarray,
        treatment: np.ndarray,
        alpha: float = 0.05,
    ) -> dict:
        """Two-sample Welch's t-test.

        Returns:
            dict with keys: method, t_statistic, p_value, effect_size,
            confidence_interval, is_significant.
        """
        control = np.asarray(control, dtype=np.float64)
        treatment = np.asarray(treatment, dtype=np.float64)

        t_stat, p_value = stats.ttest_ind(treatment, control, equal_var=False)

        mean_diff = float(np.mean(treatment) - np.mean(control))
        # Pooled std for Cohen's d
        n_c, n_t = len(control), len(treatment)
        pooled_std = math.sqrt(
            ((n_c - 1) * np.var(control, ddof=1) + (n_t - 1) * np.var(treatment, ddof=1))
            / (n_c + n_t - 2)
        )
        effect_size = mean_diff / pooled_std if pooled_std > 0 else 0.0

        # Confidence interval for the difference in means
        se_diff = math.sqrt(
            np.var(control, ddof=1) / n_c + np.var(treatment, ddof=1) / n_t
        )
        df = self._welch_degrees_of_freedom(control, treatment)
        t_crit = stats.t.ppf(1 - alpha / 2, df)
        ci_lower = mean_diff - t_crit * se_diff
        ci_upper = mean_diff + t_crit * se_diff

        is_significant = bool(p_value < alpha)

        return {
            "method": "frequentist",
            "t_statistic": float(t_stat),
            "p_value": float(p_value),
            "effect_size": float(effect_size),
            "mean_difference": float(mean_diff),
            "confidence_interval": (float(ci_lower), float(ci_upper)),
            "is_significant": is_significant,
        }

    @staticmethod
    def _welch_degrees_of_freedom(a: np.ndarray, b: np.ndarray) -> float:
        """Welch-Satterthwaite degrees of freedom."""
        n_a, n_b = len(a), len(b)
        var_a, var_b = np.var(a, ddof=1), np.var(b, ddof=1)
        num = (var_a / n_a + var_b / n_b) ** 2
        denom = (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
        if denom == 0:
            return float(n_a + n_b - 2)
        return float(num / denom)

    # ------------------------------------------------------------------
    # Bayesian (Beta-Binomial)
    # ------------------------------------------------------------------

    def bayesian_test(
        self,
        control: np.ndarray,
        treatment: np.ndarray,
        prior_alpha: float = 1.0,
        prior_beta: float = 1.0,
        n_simulations: int = 100_000,
    ) -> dict:
        """Bayesian A/B test for conversion rates using a Beta-Binomial model.

        Assumes each array contains binary outcomes (0 or 1).  Uses Monte
        Carlo simulation to estimate P(treatment > control).

        Returns:
            dict with keys: method, control_rate, treatment_rate,
            prob_treatment_better, expected_loss_control, expected_loss_treatment,
            is_significant (prob > 0.95).
        """
        control = np.asarray(control, dtype=np.float64)
        treatment = np.asarray(treatment, dtype=np.float64)

        # Posterior parameters  Beta(alpha + successes, beta + failures)
        c_successes = np.sum(control)
        c_failures = len(control) - c_successes
        t_successes = np.sum(treatment)
        t_failures = len(treatment) - t_successes

        alpha_c = prior_alpha + c_successes
        beta_c = prior_beta + c_failures
        alpha_t = prior_alpha + t_successes
        beta_t = prior_beta + t_failures

        # Monte Carlo draws
        rng = np.random.default_rng(seed=42)
        samples_c = rng.beta(alpha_c, beta_c, size=n_simulations)
        samples_t = rng.beta(alpha_t, beta_t, size=n_simulations)

        prob_treatment_better = float(np.mean(samples_t > samples_c))

        # Expected loss: E[max(control - treatment, 0)] for treatment
        diff = samples_c - samples_t
        expected_loss_treatment = float(np.mean(np.maximum(diff, 0)))
        expected_loss_control = float(np.mean(np.maximum(-diff, 0)))

        control_rate = float(np.mean(control))
        treatment_rate = float(np.mean(treatment))

        is_significant = prob_treatment_better > 0.95 or prob_treatment_better < 0.05

        return {
            "method": "bayesian",
            "control_rate": control_rate,
            "treatment_rate": treatment_rate,
            "prob_treatment_better": prob_treatment_better,
            "expected_loss_control": expected_loss_control,
            "expected_loss_treatment": expected_loss_treatment,
            "effect_size": treatment_rate - control_rate,
            "is_significant": is_significant,
        }

    # ------------------------------------------------------------------
    # Sequential testing (mSPRT)
    # ------------------------------------------------------------------

    def sequential_test(
        self,
        control: np.ndarray,
        treatment: np.ndarray,
        alpha: float = 0.05,
        tau: float = 1e-4,
    ) -> dict:
        """Always-valid sequential test using mixture sequential probability ratio test (mSPRT).

        The mSPRT provides an "always-valid" p-value that controls the
        type-I error rate no matter when you peek at the data.

        The mixing distribution is a normal prior with variance ``tau``
        on the true effect size.

        Returns:
            dict with keys: method, msprt_statistic, always_valid_p_value,
            effect_size, is_significant.
        """
        control = np.asarray(control, dtype=np.float64)
        treatment = np.asarray(treatment, dtype=np.float64)

        n_c, n_t = len(control), len(treatment)
        mean_c, mean_t = np.mean(control), np.mean(treatment)
        var_c = np.var(control, ddof=1) if n_c > 1 else 1e-10
        var_t = np.var(treatment, ddof=1) if n_t > 1 else 1e-10

        # Variance of the difference in means
        sigma_sq = var_c / n_c + var_t / n_t
        mean_diff = float(mean_t - mean_c)

        # mSPRT statistic: Lambda = sqrt(sigma^2 / (sigma^2 + tau)) *
        #   exp( tau * z^2 / (2 * sigma^2 * (sigma^2 + tau)) )
        # where z = mean_diff
        ratio = sigma_sq / (sigma_sq + tau)
        exponent = tau * mean_diff ** 2 / (2.0 * sigma_sq * (sigma_sq + tau))
        # Clamp exponent to prevent overflow
        exponent = min(exponent, 500.0)
        lambda_stat = math.sqrt(ratio) * math.exp(exponent)

        # Always-valid p-value: min(1, 1 / Lambda)
        always_valid_p = min(1.0, 1.0 / lambda_stat) if lambda_stat > 0 else 1.0

        # Effect size (Cohen's d)
        pooled_std = math.sqrt(
            ((n_c - 1) * var_c + (n_t - 1) * var_t) / (n_c + n_t - 2)
        ) if (n_c + n_t > 2) else 1.0
        effect_size = mean_diff / pooled_std if pooled_std > 0 else 0.0

        is_significant = bool(always_valid_p < alpha)

        return {
            "method": "sequential",
            "msprt_statistic": float(lambda_stat),
            "always_valid_p_value": float(always_valid_p),
            "effect_size": float(effect_size),
            "mean_difference": float(mean_diff),
            "is_significant": is_significant,
        }

    # ------------------------------------------------------------------
    # Sample size calculation
    # ------------------------------------------------------------------

    def calculate_sample_size(
        self,
        baseline_rate: float,
        mde: float,
        alpha: float = 0.05,
        power: float = 0.8,
    ) -> int:
        """Calculate a continuity-corrected prospective two-proportion target.

        The correction makes this planning helper conservative for the
        discrete fixed-horizon Fisher inference used by the experiment API.

        Args:
            baseline_rate: Expected conversion rate for the control group.
            mde: Minimum detectable effect (absolute difference in rates).
            alpha: Significance level (two-sided).
            power: Statistical power (1 - beta).

        Returns:
            Required sample size per variant (rounded up).
        """
        p1 = baseline_rate
        p2 = baseline_rate + mde
        # Pooled probability under H0
        p_bar = (p1 + p2) / 2.0

        z_alpha = stats.norm.ppf(1 - alpha / 2)
        z_beta = stats.norm.ppf(power)

        numerator = (
            z_alpha * math.sqrt(2 * p_bar * (1 - p_bar))
            + z_beta * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))
        ) ** 2
        denominator = mde ** 2

        if denominator == 0:
            return 0

        asymptotic_n = numerator / denominator
        corrected_n = (
            asymptotic_n
            / 4.0
            * (1.0 + math.sqrt(1.0 + 4.0 / (asymptotic_n * abs(mde)))) ** 2
        )
        n = math.ceil(corrected_n)
        return n
