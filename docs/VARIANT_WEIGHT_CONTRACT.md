# Weighted variant contract

APDL uses one weighted-variant contract in the Config service, JavaScript SDK,
Python SDK, and PostgreSQL:

- `MAX_VARIANTS = 10`
- `MAX_VARIANT_WEIGHT = 9_007_199_254_740_991`
- `MAX_TOTAL_VARIANT_WEIGHT = 9_007_199_254_740_991`

Every weight is a nonnegative integer that JavaScript can represent exactly.
The list is nonempty, its keys are unique, its default key exists, and its total
weight is positive without exceeding the total limit. Experiment authoring is
stricter: it requires 2–10 variants and every experiment weight is positive.

The executable cross-runtime vectors are in
`fixtures/gates/variant-weights.json`. Migration 042 applies the same bounds to
`flags.variants` and `experiments.variants_json`. It does not leave an invalid
configuration active: affected experiment bundles are stopped and disabled,
their original and repaired rows are audited, and their flag/experiment outbox
intents share one new project version. Invalid standalone flags are likewise
disabled and audited. Repairs use explicit `control` and `treatment` variants
with weight `1`; an invalid experiment without a backing flag aborts the
migration instead of allowing an untracked repair.
