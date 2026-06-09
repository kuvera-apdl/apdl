"""Cross-SDK bucketing parity.

The golden values below were produced by the canonical config service
implementation (``services/config/app/flags/evaluator.py``). If this test ever
fails, the Python SDK has drifted from the server and JS SDK — users would
bucket inconsistently across evaluation sites.
"""

import math

from apdl import hash_bucket, is_in_rollout, percentage_bucket

# (flag_key, salt, unit_id) -> (hash_bucket, percentage_bucket)
GOLDEN = {
    ("my-flag", "salt1", "user-123"): (3855598107, 89.7701389132),
    ("", "", ""): (2550542581, 59.3844470939),
    ("checkout", "s", "u"): (2818530299, 65.6240223827),
    ("🚀flag", "s🧂", "usér"): (3554166887, 82.7518964146),
    ("a", "b", "c"): (1833728151, 42.6948105783),
}


def test_hash_bucket_matches_canonical_server():
    for args, (expected_hash, _) in GOLDEN.items():
        assert hash_bucket(*args) == expected_hash, args


def test_percentage_bucket_matches_canonical_server():
    for args, (_, expected_pct) in GOLDEN.items():
        assert math.isclose(percentage_bucket(*args), expected_pct, abs_tol=1e-9), args


def test_hash_is_uint32():
    assert 0 <= hash_bucket("x", "y", "z") <= 0xFFFFFFFF


def test_is_in_rollout_bounds():
    assert is_in_rollout("k", "s", "u", 100.0) is True
    assert is_in_rollout("k", "s", "u", 0.0) is False


def test_is_in_rollout_is_deterministic_against_bucket():
    bucket = percentage_bucket("checkout", "s", "u")  # 65.62...
    assert is_in_rollout("checkout", "s", "u", bucket + 1.0) is True
    assert is_in_rollout("checkout", "s", "u", bucket - 1.0) is False
