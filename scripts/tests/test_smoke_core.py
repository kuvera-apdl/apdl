"""Dependency-free unit contracts for scripts/smoke_core.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "smoke_core.py"
SPEC = importlib.util.spec_from_file_location("apdl_smoke_core", SCRIPT)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError(f"Could not load {SCRIPT}")
smoke_core = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke_core
SPEC.loader.exec_module(smoke_core)


def make_args() -> SimpleNamespace:
    return SimpleNamespace(
        confidential_key="proj_demo_0123456789abcdef",
        browser_key="client_demo_0123456789abcdef",
        gateway_url="http://gateway.test",
        ingestion_url="http://ingestion.test",
        config_url="http://config.test",
        query_url="http://query.test",
        admin_url=None,
        request_timeout=3.0,
        pipeline_timeout=10.0,
        poll_interval=0.01,
    )


def make_state() -> smoke_core.SmokeState:
    return smoke_core.SmokeState(
        run_id="0123456789abcdef",
        flag_key="smoke_core_0123456789abcdef",
        event_name="apdl_core_smoke_0123456789abcdef",
        message_id="smoke_core_0123456789abcdef",
        event_timestamp="2026-07-13T23:59:59.999Z",
        event_date="2026-07-13",
    )


def count_response(total: int) -> dict:
    state = make_state()
    return {
        "results": [
            {
                "selector": state.event_name,
                "event_name": state.event_name,
                "event_count": total,
                "unique_users": min(total, 1),
            }
        ],
        "total_events": total,
        "total_users": min(total, 1),
    }


def flag_response(*, archived: bool = False, version: int = 7) -> dict:
    state = make_state()
    return {
        "key": state.flag_key,
        "project_id": "demo",
        "name": "APDL OSS core transport smoke",
        "state": "archived" if archived else "active",
        "owners": ["oss-smoke"],
        "review_by": None,
        "description": "Ephemeral server-evaluated release smoke flag",
        "enabled": not archived,
        "default_variant": "control",
        "variants": [
            {"key": "control", "weight": 1},
            {"key": "treatment", "weight": 0},
        ],
        "rules": [],
        "fallthrough": {
            "rollout": {"percentage": 100.0, "bucket_by": "user_id"}
        },
        "salt": "server-generated-salt",
        "evaluation_mode": "server",
        "auto_disable": False,
        "guardrails": [],
        "disabled_reason": "",
        "disabled_by": "",
        "disabled_at": None,
        "version": version,
        "created_at": "2026-07-13T00:00:00+00:00",
        "updated_at": "2026-07-13T00:01:00+00:00",
        "archived_at": "2026-07-13T00:01:00+00:00" if archived else None,
    }


class EventTransportTests(unittest.TestCase):
    def test_event_write_is_one_canonical_event_in_one_request(self) -> None:
        args = make_args()
        state = make_state()
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(202, {"accepted": 1}),
        ) as request:
            smoke_core._send_event_once(args, state)

        request.assert_called_once()
        call = request.call_args
        self.assertEqual(call.args, ("http://gateway.test/v1/events", args.browser_key))
        self.assertEqual(call.kwargs["method"], "POST")
        self.assertEqual(call.kwargs["expected_status"], {202})
        payload = call.kwargs["payload"]
        self.assertEqual(set(payload), {"events"})
        self.assertEqual(len(payload["events"]), 1)
        event = payload["events"][0]
        self.assertEqual(
            set(event),
            {
                "event",
                "type",
                "anonymous_id",
                "timestamp",
                "properties",
                "context",
                "message_id",
                "session_id",
            },
        )
        self.assertEqual(event["event"], state.event_name)
        self.assertEqual(event["type"], "track")
        self.assertEqual(event["message_id"], state.message_id)
        self.assertEqual(event["timestamp"], "2026-07-13T23:59:59.999Z")

        count = smoke_core._count_payload("demo", state.event_name, state.event_date)
        self.assertEqual(count["start_date"], "2026-07-13")
        self.assertEqual(count["end_date"], "2026-07-13")

    def test_failed_event_write_is_not_retried(self) -> None:
        args = make_args()
        with patch.object(
            smoke_core,
            "_request_json",
            side_effect=smoke_core.SmokeFailure("transport failed"),
        ) as request:
            with self.assertRaisesRegex(smoke_core.SmokeFailure, "transport failed"):
                smoke_core._send_event_once(args, make_state())
        request.assert_called_once()


class ExactCountTests(unittest.TestCase):
    def test_poll_accepts_zero_then_exactly_one(self) -> None:
        args = make_args()
        sleep = Mock()
        with patch.object(
            smoke_core,
            "_request_json",
            side_effect=[(200, count_response(0)), (200, count_response(1))],
        ) as request:
            smoke_core._poll_exact_count(
                args,
                "demo",
                make_state(),
                monotonic=lambda: 0.0,
                sleep=sleep,
            )

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(args.poll_interval)

    def test_poll_fails_immediately_on_duplicate_count(self) -> None:
        args = make_args()
        sleep = Mock()
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(200, count_response(2)),
        ) as request:
            with self.assertRaisesRegex(
                smoke_core.SmokeFailure,
                "duplicate smoke events: total_events=2",
            ):
                smoke_core._poll_exact_count(
                    args,
                    "demo",
                    make_state(),
                    monotonic=lambda: 0.0,
                    sleep=sleep,
                )

        request.assert_called_once()
        sleep.assert_not_called()

    def test_poll_rejects_a_mismatched_selector_projection(self) -> None:
        args = make_args()
        response = count_response(1)
        response["results"][0]["selector"] = "different-event"
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(200, response),
        ):
            with self.assertRaisesRegex(
                smoke_core.SmokeFailure,
                "Query selector count differs",
            ):
                smoke_core._poll_exact_count(args, "demo", make_state())


class ResponseContractTests(unittest.TestCase):
    def test_create_captures_returned_version_and_server_contract(self) -> None:
        args = make_args()
        state = make_state()
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(201, {"created": True, "flag": flag_response()}),
        ) as request:
            smoke_core._create_flag(args, "demo", state)

        self.assertEqual(state.created_version, 7)
        payload = request.call_args.kwargs["payload"]
        self.assertEqual(payload["evaluation_mode"], "server")
        self.assertIs(payload["auto_disable"], False)
        self.assertEqual(payload["variants"][1]["weight"], 0)

    def test_evaluation_is_strict_and_does_not_log_an_exposure(self) -> None:
        args = make_args()
        state = make_state()
        state.created_version = 7
        response = {
            "key": state.flag_key,
            "variant": "control",
            "reason": "fallthrough",
            "rule_id": None,
            "rollout_bucket": 10.0,
            "variant_bucket": 20.0,
            "rollout_percentage": 100.0,
            "bucket_by": "user_id",
            "config_version": 7,
            "source": "server",
        }
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(200, response),
        ) as request:
            smoke_core._evaluate_flag(args, "demo", state)

        self.assertIs(request.call_args.kwargs["payload"]["log_exposure"], False)

    def test_archive_uses_the_create_version_and_validates_tombstone(self) -> None:
        args = make_args()
        state = make_state()
        state.created_version = 7
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(
                200,
                {"archived": True, "flag": flag_response(archived=True, version=8)},
            ),
        ) as request:
            smoke_core._archive_flag(args, state)

        self.assertEqual(request.call_args.kwargs["method"], "DELETE")
        self.assertTrue(request.call_args.args[0].endswith("?version=7"))

    def test_create_contract_drift_still_retains_cleanup_version(self) -> None:
        args = make_args()
        state = make_state()
        response = {"created": True, "flag": flag_response(), "unexpected": True}
        with patch.object(
            smoke_core,
            "_request_json",
            return_value=(201, response),
        ):
            with self.assertRaisesRegex(smoke_core.SmokeFailure, "fields differ"):
                smoke_core._create_flag(args, "demo", state)

        self.assertEqual(state.created_version, 7)


class CleanupTests(unittest.TestCase):
    def test_cleanup_failure_does_not_replace_primary_failure(self) -> None:
        args = make_args()
        primary = smoke_core.SmokeFailure("primary failure")

        def fail_after_create(_, __, state):
            state.created_version = 7
            raise primary

        with (
            patch.object(smoke_core, "_exercise_core", side_effect=fail_after_create),
            patch.object(
                smoke_core,
                "_archive_flag",
                side_effect=smoke_core.SmokeFailure("cleanup failure"),
            ) as cleanup,
            self.assertRaises(smoke_core.SmokeFailure) as raised,
        ):
            smoke_core._run(args)

        self.assertIs(raised.exception, primary)
        self.assertIn("cleanup failure", " ".join(primary.__notes__))
        cleanup.assert_called_once()


class CredentialTests(unittest.TestCase):
    def test_credentials_must_be_strict_and_project_matched(self) -> None:
        self.assertEqual(
            smoke_core._project_id(
                "proj_demo_0123456789abcdef",
                "client_demo_0123456789abcdef",
            ),
            "demo",
        )
        with self.assertRaisesRegex(smoke_core.SmokeFailure, "same project"):
            smoke_core._project_id(
                "proj_demo_0123456789abcdef",
                "client_other_0123456789abcdef",
            )


if __name__ == "__main__":
    unittest.main()
