#!/usr/bin/env python3
"""Black-box smoke test for authoritative experiment analysis.

The script deliberately uses only public HTTP boundaries for the functional
test: Config creates and projects the experiment, Ingestion accepts canonical
events, the Redis/ClickHouse pipeline materializes them, and Query delegates
the caller credential back to Config before executing its production SQL.

The ClickHouse HTTP cleanup boundary is required: every run removes its
immutable event and exposure rows after Config archives the unique experiment.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "fixtures" / "experiments" / "three-arm-analysis.json"
CONFIDENTIAL_KEY = re.compile(
    r"^proj_(?P<project_id>[A-Za-z0-9]{1,64})_[A-Za-z0-9]{16,128}$"
)


class SmokeFailure(RuntimeError):
    """A functional assertion or required cleanup failed."""


@dataclass(frozen=True)
class Identity:
    field: str
    value: str

    def envelope(self) -> dict[str, str]:
        return {self.field: self.value}


def _iso_milliseconds(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _assert_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise SmokeFailure(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SmokeFailure(
            f"{label} fields differ (missing={missing}, extra={extra})"
        )


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def _request_json(
    url: str,
    api_key: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    expected_status: set[int] | frozenset[int] = frozenset({200}),
    timeout: float = 10.0,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json", "X-API-Key": api_key}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        status = exc.code
        body = exc.read()
    except URLError as exc:
        raise SmokeFailure(f"{method} {url} failed: {exc.reason}") from exc

    try:
        decoded = json.loads(body) if body else None
    except json.JSONDecodeError as exc:
        preview = body.decode("utf-8", errors="replace")[:500]
        raise SmokeFailure(
            f"{method} {url} returned non-JSON status {status}: {preview!r}"
        ) from exc

    if status not in expected_status:
        raise SmokeFailure(
            f"{method} {url} returned {status}, expected "
            f"{sorted(expected_status)}: {decoded!r}"
        )
    return status, decoded


def _load_fixture(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
        contract = fixture["config_contract"]
        runtime = fixture["runtime_smoke"]
        variants = contract["variants"]
        expected = runtime["expected"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SmokeFailure(f"Invalid runtime fixture {path}: {exc}") from exc

    if not isinstance(variants, list) or len(variants) < 2:
        raise SmokeFailure("Fixture config_contract.variants must list at least two arms")
    if contract["control_variant"] not in variants:
        raise SmokeFailure("Fixture control_variant must be a declared variant")
    if runtime["unknown_variant"] in variants:
        raise SmokeFailure("Fixture unknown_variant must not be a declared variant")
    _assert_equal(
        set(expected["arm_sample_sizes"]),
        set(variants),
        "runtime expected sample-size variants",
    )
    _assert_equal(
        set(expected["arm_conversions"]),
        set(variants),
        "runtime expected conversion variants",
    )
    return contract, runtime


def _build_events(
    *,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    run_id: str,
    flag_key: str,
    config_version: int,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    variants = list(contract["variants"])
    metric_event = str(contract["metric_event"])
    actors_per_arm = int(runtime["actors_per_declared_arm"])
    unknown_variant = str(runtime["unknown_variant"])
    same_identity = runtime["same_raw_identity"]
    message_prefix = f"smoke_{run_id}_"
    events: list[dict[str, Any]] = []
    event_counter = 0

    def message_id(label: str) -> str:
        nonlocal event_counter
        event_counter += 1
        return f"{message_prefix}{event_counter:03d}_{label}"

    def exposure(identity: Identity, variant: str, at: datetime, label: str) -> None:
        events.append(
            {
                "event": "$feature_flag_exposure",
                "type": "track",
                **identity.envelope(),
                "session_id": f"session_{run_id}_{event_counter + 1:03d}",
                "message_id": message_id(label),
                "timestamp": _iso_milliseconds(at),
                "context": {},
                "properties": {
                    "flag_key": flag_key,
                    "variant": variant,
                    "reason": "fallthrough",
                    "rule_id": None,
                    "rollout_bucket": 1.0,
                    "variant_bucket": 1.0,
                    "rollout_percentage": 100.0,
                    "bucket_by": identity.field,
                    "config_version": config_version,
                    "source": "initial_fetch",
                    "page": "/smoke/experiment-analysis",
                    "component": "ExperimentAnalysisSmoke",
                },
            }
        )

    def metric(identity: Identity, at: datetime, label: str) -> None:
        events.append(
            {
                "event": metric_event,
                "type": "track",
                **identity.envelope(),
                "message_id": message_id(label),
                "timestamp": _iso_milliseconds(at),
                "context": {},
                "properties": {"source": "experiment-analysis-smoke"},
            }
        )

    identities: dict[str, list[Identity]] = {}
    exposure_times: dict[tuple[str, int], datetime] = {}
    for variant_index, variant in enumerate(variants):
        arm: list[Identity] = []
        for actor_index in range(actors_per_arm):
            if actor_index == 0 and variant == same_identity["user_variant"]:
                identity = Identity("user_id", f"{run_id}-{same_identity['raw_id']}")
            elif actor_index == 0 and variant == same_identity["anonymous_variant"]:
                identity = Identity(
                    "anonymous_id", f"{run_id}-{same_identity['raw_id']}"
                )
            else:
                identity = Identity(
                    "user_id", f"{run_id}-{variant}-{actor_index:02d}"
                )
            assigned_at = start + timedelta(
                milliseconds=(variant_index * actors_per_arm) + actor_index
            )
            arm.append(identity)
            exposure_times[(variant, actor_index)] = assigned_at
            exposure(
                identity,
                variant,
                assigned_at,
                f"assigned_{variant}_{actor_index:02d}",
            )
        identities[variant] = arm

    unknown_identities = [
        Identity("user_id", f"{run_id}-{unknown_variant}-{index:02d}")
        for index in range(int(runtime["unknown_actor_count"]))
    ]
    for index, identity in enumerate(unknown_identities):
        exposure(
            identity,
            unknown_variant,
            start + timedelta(milliseconds=500 + index),
            f"assigned_{unknown_variant}_{index:02d}",
        )

    for index, crossover in enumerate(runtime["crossovers"]):
        first_variant = str(crossover["first_variant"])
        actor_index = int(crossover["actor_index"])
        if first_variant == unknown_variant:
            identity = unknown_identities[actor_index]
        else:
            identity = identities[first_variant][actor_index]
        exposure(
            identity,
            str(crossover["later_variant"]),
            start + timedelta(seconds=2, milliseconds=index),
            f"crossover_{index:02d}",
        )

    pre_metric = runtime["pre_exposure_metric"]
    pre_variant = str(pre_metric["variant"])
    pre_index = int(pre_metric["actor_index"])
    metric(
        identities[pre_variant][pre_index],
        exposure_times[(pre_variant, pre_index)] - timedelta(milliseconds=1),
        "metric_before_assignment",
    )

    boundaries = runtime["boundary_cases"]
    before_identity = Identity("user_id", f"{run_id}-before-start")
    before_start = start - timedelta(milliseconds=1)
    exposure(
        before_identity,
        str(boundaries["before_start_variant"]),
        before_start,
        "exposure_before_start",
    )
    metric(before_identity, before_start, "metric_before_start")

    last_identity = Identity("user_id", f"{run_id}-last-before-end")
    last_before_end = end - timedelta(milliseconds=1)
    exposure(
        last_identity,
        str(boundaries["last_before_end_variant"]),
        last_before_end,
        "exposure_last_before_end",
    )
    metric(last_identity, last_before_end, "metric_last_before_end")

    at_end_identity = Identity("user_id", f"{run_id}-at-end")
    exposure(
        at_end_identity,
        str(boundaries["at_end_variant"]),
        end,
        "exposure_at_end",
    )
    # Use an already-assigned declared actor so an inclusive upper metric bound
    # would visibly violate the fixture's all-zero conversion assertion.
    metric(identities[variants[-1]][0], end, "metric_at_end")

    _assert_equal(
        len(events), runtime["expected"]["accepted_events"], "generated event count"
    )
    return events


def _assert_projection(
    projection: dict[str, Any],
    *,
    experiment_key: str,
    flag_key: str,
    contract: dict[str, Any],
    start: datetime,
    end: datetime,
    version: int,
    expected_status: str,
) -> None:
    _assert_keys(
        projection,
        {
            "key",
            "flag_key",
            "status",
            "control_variant",
            "variants",
            "metric_event",
            "metric_direction",
            "statistical_plan",
            "start_date",
            "end_date",
            "version",
        },
        "Config analysis projection",
    )
    _assert_equal(projection["key"], experiment_key, "projected experiment key")
    _assert_equal(projection["flag_key"], flag_key, "projected flag key")
    _assert_equal(projection["status"], expected_status, "projected status")
    _assert_equal(
        projection["control_variant"],
        contract["control_variant"],
        "projected control",
    )
    _assert_equal(projection["variants"], contract["variants"], "projected variants")
    _assert_equal(
        projection["metric_event"], contract["metric_event"], "projected metric"
    )
    _assert_equal(
        projection["metric_direction"], contract["metric_direction"], "metric direction"
    )
    _assert_equal(
        projection["statistical_plan"], contract["statistical_plan"], "statistical plan"
    )
    _assert_equal(_parse_instant(projection["start_date"]), start, "projected start")
    _assert_equal(_parse_instant(projection["end_date"]), end, "projected end")
    _assert_equal(projection["version"], version, "projected version")


def _assert_analysis(
    result: dict[str, Any],
    *,
    experiment_key: str,
    flag_key: str,
    contract: dict[str, Any],
    runtime: dict[str, Any],
    start: datetime,
    end: datetime,
    version: int,
) -> None:
    expected = runtime["expected"]
    _assert_keys(
        result,
        {
            "experiment_key",
            "flag_key",
            "experiment_status",
            "control_variant",
            "metric_event",
            "metric_direction",
            "statistical_plan",
            "start_date",
            "end_date",
            "config_version",
            "arms",
            "crossover_actors",
            "unknown_variant_actors",
            "identity_conflict_actors",
            "identity_quality",
            "analysis_status",
            "data_completeness",
            "deployment_readiness",
            "inference_method",
            "interval_method",
            "correction",
            "comparisons",
        },
        "Query experiment analysis",
    )
    _assert_equal(result["analysis_status"], "decision_snapshot", "analysis status")
    _assert_equal(result["experiment_key"], experiment_key, "analysis experiment key")
    _assert_equal(result["flag_key"], flag_key, "analysis flag key")
    _assert_equal(result["experiment_status"], "completed", "analysis experiment status")
    _assert_equal(
        result["control_variant"], contract["control_variant"], "analysis control"
    )
    _assert_equal(result["metric_event"], contract["metric_event"], "analysis metric")
    _assert_equal(result["metric_direction"], contract["metric_direction"], "metric direction")
    _assert_equal(result["statistical_plan"], contract["statistical_plan"], "statistical plan")
    _assert_equal(_parse_instant(result["start_date"]), start, "analysis start")
    _assert_equal(_parse_instant(result["end_date"]), end, "analysis end")
    _assert_equal(result["config_version"], version, "analysis config version")

    arms = result["arms"]
    _assert_equal(
        [arm["variant"] for arm in arms], contract["variants"], "analysis arm order"
    )
    for arm in arms:
        _assert_keys(
            arm,
            {"variant", "sample_size", "conversions", "conversion_rate"},
            f"analysis arm {arm.get('variant')}",
        )
        variant = arm["variant"]
        _assert_equal(
            arm["sample_size"], expected["arm_sample_sizes"][variant], f"{variant} sample size"
        )
        _assert_equal(
            arm["conversions"], expected["arm_conversions"][variant], f"{variant} conversions"
        )
        _assert_equal(arm["conversion_rate"], 0.0, f"{variant} conversion rate")

    _assert_equal(
        result["crossover_actors"], expected["crossover_actors"], "crossover actors"
    )
    _assert_equal(
        result["unknown_variant_actors"],
        expected["unknown_variant_actors"],
        "unknown-variant actors",
    )
    _assert_equal(
        result["identity_conflict_actors"],
        expected["identity_conflict_actors"],
        "identity-conflict actors",
    )
    _assert_equal(result["identity_quality"], "unambiguous", "identity quality")
    _assert_equal(result["data_completeness"], "not_verified", "data completeness")
    _assert_equal(result["deployment_readiness"], "not_assessed", "deployment readiness")
    _assert_equal(result["inference_method"], "fisher_exact_two_sided", "inference method")
    _assert_equal(result["interval_method"], "newcombe_wilson", "interval method")
    _assert_equal(result["correction"], "bonferroni", "multiple-test correction")

    comparisons = result["comparisons"]
    treatment_order = [
        variant
        for variant in contract["variants"]
        if variant != contract["control_variant"]
    ]
    _assert_equal(
        [comparison["treatment_variant"] for comparison in comparisons],
        treatment_order,
        "comparison order",
    )
    for comparison in comparisons:
        _assert_keys(
            comparison,
            {
                "control_variant",
                "treatment_variant",
                "control_rate",
                "treatment_rate",
                "rate_difference",
                "confidence_interval",
                "raw_p_value",
                "adjusted_p_value",
                "is_statistically_significant",
            },
            f"comparison {comparison.get('treatment_variant')}",
        )
        _assert_equal(
            comparison["control_variant"], contract["control_variant"], "comparison control"
        )
        _assert_equal(comparison["control_rate"], 0.0, "comparison control rate")
        _assert_equal(comparison["treatment_rate"], 0.0, "comparison treatment rate")
        _assert_equal(comparison["rate_difference"], 0.0, "comparison rate difference")
        lower, upper = comparison["confidence_interval"]
        if not lower < 0.0 < upper:
            raise SmokeFailure(
                "all-zero Newcombe/Wilson interval must be finite and span zero: "
                f"{comparison['confidence_interval']!r}"
            )
        _assert_equal(
            comparison["raw_p_value"], expected["raw_p_value"], "raw p-value"
        )
        _assert_equal(
            comparison["adjusted_p_value"],
            expected["adjusted_p_value"],
            "adjusted p-value",
        )
        _assert_equal(
            comparison["is_statistically_significant"],
            False,
            "significance verdict",
        )


def _clickhouse_request(
    base_url: str,
    query: str,
    parameters: dict[str, str],
    *,
    user: str,
    password: str,
    timeout: float,
) -> None:
    parts = urlsplit(base_url)
    existing = dict(
        item.split("=", 1) if "=" in item else (item, "")
        for item in parts.query.split("&")
        if item
    )
    existing.update({"database": "apdl", "mutations_sync": "2"})
    existing.update({f"param_{key}": value for key, value in parameters.items()})
    url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(existing), ""))
    headers = {"Content-Type": "text/plain; charset=utf-8"}
    if user or password:
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = Request(url, data=query.encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.status != 200:
                raise SmokeFailure(
                    f"ClickHouse cleanup returned {response.status}: "
                    f"{body.decode('utf-8', errors='replace')[:500]}"
                )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise SmokeFailure(f"ClickHouse cleanup returned {exc.code}: {body}") from exc
    except URLError as exc:
        raise SmokeFailure(f"ClickHouse cleanup failed: {exc.reason}") from exc


def _cleanup_clickhouse(
    base_url: str,
    *,
    project_id: str,
    flag_key: str,
    message_prefix: str,
    user: str,
    password: str,
    timeout: float,
) -> None:
    _clickhouse_request(
        base_url,
        (
            "ALTER TABLE feature_flag_exposures DELETE WHERE "
            "project_id = {project_id:String} AND flag_key = {flag_key:String}"
        ),
        {"project_id": project_id, "flag_key": flag_key},
        user=user,
        password=password,
        timeout=timeout,
    )
    _clickhouse_request(
        base_url,
        (
            "ALTER TABLE events DELETE WHERE project_id = {project_id:String} "
            "AND startsWith(message_id, {message_prefix:String})"
        ),
        {"project_id": project_id, "message_prefix": message_prefix},
        user=user,
        password=password,
        timeout=timeout,
    )


def _run(args: argparse.Namespace) -> None:
    match = CONFIDENTIAL_KEY.fullmatch(args.api_key or "")
    if match is None:
        raise SmokeFailure(
            "--api-key/APDL_DEV_API_KEY must be a confidential "
            "proj_{project_id}_{secret} credential"
        )
    project_id = match.group("project_id")
    contract, runtime = _load_fixture(args.fixture)
    run_id = uuid.uuid4().hex[:12]
    experiment_key = f"smoke_analysis_{run_id}"
    flag_key = f"smoke_flag_{run_id}"
    message_prefix = f"smoke_{run_id}_"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(minutes=10)
    end = now + timedelta(seconds=10)
    experiment_path = f"/v1/experiments/{quote(experiment_key, safe='')}/analysis"
    admin_experiment_path = f"/v1/admin/experiments/{quote(experiment_key, safe='')}"
    query_path = f"/v1/query/experiment/{quote(experiment_key, safe='')}"
    created_version: int | None = None
    primary_failure: Exception | None = None
    cleanup_failures: list[str] = []

    print(f"Experiment-analysis smoke run {run_id} for project {project_id}")
    try:
        for name, url in (
            ("Ingestion", _join_url(args.ingestion_url, "/health")),
            ("Config", _join_url(args.config_url, "/ready")),
            ("Query", _join_url(args.query_url, "/health")),
        ):
            _request_json(url, args.api_key, expected_status={200}, timeout=args.request_timeout)
            print(f"  ok  {name} health")

        create_payload = {
            "key": experiment_key,
            "flag_key": flag_key,
            "status": "running",
            "description": "Cross-service authoritative analysis smoke fixture",
            "traffic_percentage": 100.0,
            "start_date": _iso_milliseconds(start),
            "end_date": _iso_milliseconds(end),
            "variants": [
                {"key": variant, "weight": 1, "description": ""}
                for variant in contract["variants"]
            ],
            "default_variant": contract["control_variant"],
            "primary_metric": {
                "event": contract["metric_event"],
                "type": "conversion",
                "direction": contract["metric_direction"],
            },
            "statistical_plan": contract["statistical_plan"],
            "targeting_rules": [],
        }
        _, created = _request_json(
            _join_url(args.config_url, "/v1/admin/experiments"),
            args.api_key,
            method="POST",
            payload=create_payload,
            expected_status={201},
            timeout=args.request_timeout,
        )
        _assert_equal(created["created"], True, "Config create acknowledgement")
        _assert_equal(created["key"], experiment_key, "created experiment key")
        _assert_equal(created["flag_key"], flag_key, "created flag key")
        created_version = int(created["version"])
        print("  ok  Config created experiment and backing flag atomically")

        _, projection = _request_json(
            _join_url(args.config_url, experiment_path),
            args.api_key,
            timeout=args.request_timeout,
        )
        _assert_projection(
            projection,
            experiment_key=experiment_key,
            flag_key=flag_key,
            contract=contract,
            start=start,
            end=end,
            version=created_version,
            expected_status="running",
        )
        print("  ok  Config returned the strict authoritative projection")

        _request_json(
            _join_url(args.config_url, f"{experiment_path}?metric_event=caller_override"),
            args.api_key,
            expected_status={422},
            timeout=args.request_timeout,
        )
        _request_json(
            _join_url(args.query_url, f"{query_path}?metric_event=caller_override"),
            args.api_key,
            expected_status={422},
            timeout=args.request_timeout,
        )
        print("  ok  Config and Query rejected caller-controlled analysis inputs")

        events = _build_events(
            contract=contract,
            runtime=runtime,
            run_id=run_id,
            flag_key=flag_key,
            config_version=created_version,
            start=start,
            end=end,
        )
        _, accepted = _request_json(
            _join_url(args.ingestion_url, "/v1/events"),
            args.api_key,
            method="POST",
            payload={"events": events},
            expected_status={202},
            timeout=args.request_timeout,
        )
        _assert_equal(
            accepted,
            {"accepted": runtime["expected"]["accepted_events"]},
            "Ingestion acknowledgement",
        )
        print(f"  ok  Ingestion atomically accepted {len(events)} canonical events")

        _, provisional = _request_json(
            _join_url(args.query_url, query_path),
            args.api_key,
            expected_status={200},
            timeout=args.request_timeout,
        )
        _assert_equal(provisional["analysis_status"], "non_final", "running analysis state")
        _assert_equal(provisional["reason"], "experiment_running", "running analysis reason")
        if "comparisons" in provisional:
            raise SmokeFailure("running experiment unexpectedly exposed snapshot comparisons")
        print("  ok  Query withheld fixed-horizon comparisons while the experiment was running")

        wait_seconds = (end - datetime.now(timezone.utc)).total_seconds()
        if wait_seconds > 0:
            time.sleep(wait_seconds + 0.05)
        transition_status, transitioned = _request_json(
            _join_url(args.config_url, admin_experiment_path),
            args.api_key,
            method="PUT",
            payload={"version": created_version, "status": "completed"},
            expected_status={200, 409},
            timeout=args.request_timeout,
        )
        if transition_status == 200:
            _assert_equal(transitioned["updated"], True, "Config completion acknowledgement")
            created_version = int(transitioned["version"])

        _, projection = _request_json(
            _join_url(args.config_url, experiment_path),
            args.api_key,
            timeout=args.request_timeout,
        )
        if transition_status == 409:
            _assert_equal(projection["status"], "completed", "scheduler completion race")
            created_version = int(projection["version"])
        _assert_projection(
            projection,
            experiment_key=experiment_key,
            flag_key=flag_key,
            contract=contract,
            start=start,
            end=end,
            version=created_version,
            expected_status="completed",
        )
        print("  ok  Config preserved the predeclared horizon at completion")

        deadline = time.monotonic() + args.pipeline_timeout
        last_result: Any = None
        while time.monotonic() < deadline:
            _, last_result = _request_json(
                _join_url(args.query_url, query_path),
                args.api_key,
                expected_status={200},
                timeout=args.request_timeout,
            )
            try:
                _assert_analysis(
                    last_result,
                    experiment_key=experiment_key,
                    flag_key=flag_key,
                    contract=contract,
                    runtime=runtime,
                    start=start,
                    end=end,
                    version=created_version,
                )
                break
            except SmokeFailure:
                time.sleep(args.poll_interval)
        else:
            raise SmokeFailure(
                "Pipeline did not produce the exact experiment analysis before "
                f"the {args.pipeline_timeout:.1f}s deadline; last response={last_result!r}"
            )
        print(
            "  ok  Query delegated auth to Config and executed the production "
            "ClickHouse analysis"
        )
        print(
            "  ok  first assignment, namespaced identities, non-first control, "
            "multi-treatment, unknown arms, all-zero statistics, and millisecond "
            "half-open boundaries"
        )
    except Exception as exc:  # preserve the primary failure through cleanup
        primary_failure = exc
    finally:
        if created_version is not None:
            try:
                delete_url = _join_url(
                    args.config_url,
                    f"/v1/admin/experiments/{quote(experiment_key, safe='')}?"
                    + urlencode({"version": created_version}),
                )
                _, deleted = _request_json(
                    delete_url,
                    args.api_key,
                    method="DELETE",
                    expected_status={200},
                    timeout=args.request_timeout,
                )
                _assert_equal(deleted["deleted"], True, "Config cleanup acknowledgement")
                _assert_equal(deleted["key"], experiment_key, "deleted experiment key")
                _request_json(
                    _join_url(args.config_url, experiment_path),
                    args.api_key,
                    expected_status={404},
                    timeout=args.request_timeout,
                )
                print("  ok  Config deleted the experiment and archived its backing flag")
            except Exception as exc:
                cleanup_failures.append(f"Config cleanup: {exc}")

        try:
            _cleanup_clickhouse(
                args.clickhouse_cleanup_url,
                project_id=project_id,
                flag_key=flag_key,
                message_prefix=message_prefix,
                user=args.clickhouse_user,
                password=args.clickhouse_password,
                timeout=args.request_timeout,
            )
            print("  ok  ClickHouse removed the smoke event rows")
        except Exception as exc:
            cleanup_failures.append(f"ClickHouse cleanup: {exc}")

    if cleanup_failures:
        cleanup_message = "; ".join(cleanup_failures)
        if primary_failure is not None:
            print(f"Cleanup also failed: {cleanup_message}", file=sys.stderr)
        else:
            raise SmokeFailure(cleanup_message)
    if primary_failure is not None:
        if isinstance(primary_failure, SmokeFailure):
            raise primary_failure
        raise SmokeFailure(str(primary_failure)) from primary_failure
    print("Experiment-analysis smoke passed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key", default=os.environ.get("APDL_DEV_API_KEY"))
    parser.add_argument(
        "--ingestion-url",
        default=os.environ.get("APDL_INGESTION_URL", "http://localhost:8080"),
    )
    parser.add_argument(
        "--config-url",
        default=os.environ.get("APDL_CONFIG_URL", "http://localhost:8081"),
    )
    parser.add_argument(
        "--query-url",
        default=os.environ.get("APDL_QUERY_URL", "http://localhost:8082"),
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--request-timeout", type=float, default=10.0)
    parser.add_argument("--pipeline-timeout", type=float, default=45.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--clickhouse-cleanup-url",
        default=os.environ.get(
            "APDL_SMOKE_CLICKHOUSE_HTTP_URL", "http://localhost:8123"
        ),
        help="Required ClickHouse HTTP URL used to delete immutable smoke rows",
    )
    parser.add_argument(
        "--clickhouse-user",
        default=os.environ.get("CLICKHOUSE_USER", "apdl"),
    )
    parser.add_argument(
        "--clickhouse-password",
        default=os.environ.get("CLICKHOUSE_PASSWORD", "apdl_dev"),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (
        args.request_timeout <= 0
        or args.pipeline_timeout <= 0
        or args.poll_interval <= 0
        or not args.clickhouse_cleanup_url
    ):
        print(
            "Smoke failed: timeout values must be positive and ClickHouse cleanup URL is required",
            file=sys.stderr,
        )
        return 2
    try:
        _run(args)
    except SmokeFailure as exc:
        print(f"Smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
