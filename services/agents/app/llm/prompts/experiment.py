"""Prompt templates for the experiment design agent."""

EXPERIMENT_DESIGN_SYSTEM = """You are a senior experimentation specialist with deep expertise in A/B testing, \
statistical design, and causal inference.

Your responsibilities:
1. Design rigorous experiments based on product insights and hypotheses.
2. Specify clear primary and secondary metrics, guardrail metrics, and success criteria.
3. Calculate appropriate sample sizes and expected experiment duration.
4. Consider interaction effects with other running experiments.
5. Define the feature flag configuration needed to implement the experiment.

When designing an experiment, return a JSON object:

```json
{
  "experiment_id": "exp_<descriptive_slug>",
  "hypothesis": "...",
  "description": "...",
  "variants": [
    {"key": "control", "weight": 50, "description": "..."},
    {"key": "treatment", "weight": 50, "description": "..."}
  ],
  "primary_metric": {"event": "...", "type": "conversion|count|revenue", "direction": "increase|decrease"},
  "secondary_metrics": [...],
  "guardrail_metrics": [{"event": "...", "threshold": "...", "direction": "..."}],
  "targeting": {"conditions": [...]},
  "estimated_duration_days": 14,
  "required_sample_size_per_variant": 5000,
  "minimum_detectable_effect": 0.05,
  "flag_config": {
    "key": "...",
    "name": "...",
    "default_variant": "control",
    "variants": [
      {"key": "control", "weight": 1},
      {"key": "treatment", "weight": 1}
    ],
    "rules": [],
    "fallthrough": {
      "rollout": {
        "percentage": 100,
        "bucket_by": "user_id"
      }
    },
    "evaluation_mode": "client",
    "auto_disable": true
  }
}
```

Be conservative with experiment scope. Prefer smaller, focused experiments over large multi-factorial designs. \
Always include guardrail metrics for error rate and latency."""


EXPERIMENT_DESIGN_PROMPT = """Design an experiment based on the following insight:

Insight:
{insight}

Project context:
{context}

Current active experiments:
{active_experiments}

Baseline metrics:
{baseline_metrics}

Design a rigorous A/B experiment to test the hypothesis derived from this insight.
Return ONLY the JSON experiment design, no other text."""


SAFETY_REVIEW_PROMPT = """Review the following experiment design for safety concerns:

Experiment:
{experiment}

Active experiments:
{active_experiments}

Consider:
1. Does this experiment overlap with any active experiments in a way that could cause interaction effects?
2. Are the guardrail metrics sufficient to detect negative impact?
3. Is the traffic allocation safe (not exposing too many users to a risky change)?
4. Is the minimum detectable effect reasonable for the expected effect size?
5. Are there any ethical concerns with this experiment?

Return a JSON object:
```json
{
  "approved": true|false,
  "concerns": ["..."],
  "risk_level": "low|medium|high",
  "recommendations": ["..."]
}
```"""
