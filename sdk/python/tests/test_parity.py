"""Cross-language parity against the shared fixture.

``fixtures/gates/parity.json`` is consumed by the JS SDK and the config service
too. If these tests fail, the Python SDK has drifted from the canonical
bucketing or evaluation semantics — users would bucket or assign inconsistently
across evaluation sites.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from apdl import hash_bucket, percentage_bucket
from apdl.flags.cache import FlagCache
from apdl.flags.evaluator import FlagEvaluator
from apdl.flags.models import EvalContext, GateConfig

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "gates" / "parity.json"
DATA = json.loads(FIXTURE.read_text())

# Detail fields the fixture pins (``source`` is evaluation-site specific).
EVAL_FIELDS = [
    "key",
    "variant",
    "reason",
    "rule_id",
    "rollout_bucket",
    "variant_bucket",
    "rollout_percentage",
    "bucket_by",
    "config_version",
]


@pytest.mark.parametrize("case", DATA["hash_cases"], ids=lambda c: repr(c["unit_id"]))
def test_hash_parity(case):
    assert hash_bucket(case["flag_key"], case["salt"], case["unit_id"]) == case["hash"]
    assert math.isclose(
        percentage_bucket(case["flag_key"], case["salt"], case["unit_id"]),
        case["bucket"],
        abs_tol=1e-9,
    )


@pytest.mark.parametrize("case", DATA["evaluation_cases"], ids=lambda c: c["name"])
def test_evaluation_parity(case):
    flag = GateConfig.model_validate(case["flag"])
    cache = FlagCache()
    cache.set([flag], "memory")
    result = FlagEvaluator(cache).evaluate(flag.key, EvalContext(**case["context"]))
    expected = case["result"]

    for field in EVAL_FIELDS:
        got = getattr(result, field)
        want = expected.get(field)
        if (
            field in ("rollout_bucket", "variant_bucket", "rollout_percentage")
            and got is not None
            and want is not None
        ):
            assert math.isclose(got, want, abs_tol=1e-9), field
        else:
            assert got == want, f"{field}: {got!r} != {want!r}"
