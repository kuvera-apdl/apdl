"""Regression coverage for the continuity-corrected nominal planner."""

from math import comb

from app.models.schemas import prospective_sample_size_per_arm


def _binomial_probability(successes: int, sample_size: int, rate: float) -> float:
    return (
        comb(sample_size, successes)
        * rate**successes
        * (1.0 - rate) ** (sample_size - successes)
    )


def _two_sided_fisher_power(
    sample_size: int,
    control_rate: float,
    treatment_rate: float,
    alpha: float,
) -> float:
    """Enumerate the exact conditional rejection region and alternative mass."""
    power = 0.0
    denominator = comb(2 * sample_size, sample_size)
    for total_successes in range(2 * sample_size + 1):
        minimum_control = max(0, total_successes - sample_size)
        maximum_control = min(sample_size, total_successes)
        null_probabilities = {
            control_successes: (
                comb(total_successes, control_successes)
                * comb(
                    2 * sample_size - total_successes,
                    sample_size - control_successes,
                )
                / denominator
            )
            for control_successes in range(minimum_control, maximum_control + 1)
        }
        for control_successes, observed_probability in null_probabilities.items():
            p_value = sum(
                probability
                for probability in null_probabilities.values()
                if probability <= observed_probability * (1.0 + 1e-12)
            )
            if p_value >= alpha:
                continue
            treatment_successes = total_successes - control_successes
            power += _binomial_probability(
                control_successes,
                sample_size,
                control_rate,
            ) * _binomial_probability(
                treatment_successes,
                sample_size,
                treatment_rate,
            )
    return power


def test_planner_corrects_sparse_fisher_underpower():
    sample_size = prospective_sample_size_per_arm(
        baseline_conversion_rate=0.5,
        minimum_detectable_effect=0.5,
        significance_level=0.05,
        nominal_power=0.8,
        treatment_count=1,
        direction="increase",
    )

    assert sample_size == 15
    assert _two_sided_fisher_power(sample_size, 0.5, 1.0, 0.05) >= 0.8


def test_reviewed_reference_case_exceeds_nominal_power():
    sample_size = prospective_sample_size_per_arm(
        baseline_conversion_rate=0.5,
        minimum_detectable_effect=0.2,
        significance_level=0.05,
        nominal_power=0.8,
        treatment_count=1,
        direction="increase",
    )

    assert sample_size == 103
    assert _two_sided_fisher_power(sample_size, 0.5, 0.7, 0.05) >= 0.8


def test_planner_bonferroni_adjusts_for_multiple_treatments():
    one_treatment = prospective_sample_size_per_arm(
        baseline_conversion_rate=0.5,
        minimum_detectable_effect=0.5,
        significance_level=0.05,
        nominal_power=0.8,
        treatment_count=1,
        direction="increase",
    )
    two_treatments = prospective_sample_size_per_arm(
        baseline_conversion_rate=0.5,
        minimum_detectable_effect=0.5,
        significance_level=0.05,
        nominal_power=0.8,
        treatment_count=2,
        direction="increase",
    )

    assert one_treatment == 15
    assert two_treatments == 17
    assert two_treatments > one_treatment
    assert _two_sided_fisher_power(two_treatments, 0.5, 1.0, 0.025) >= 0.8
