"""Execute or validate credential-minimal offline/shadow evaluation artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.editor.environment import codegen_behavior_configuration_sha256
from app.evaluations.corpus import DEFAULT_CORPUS_PATH, load_corpus
from app.evaluations.docker_executor import DockerEvaluationExecutor
from app.evaluations.execution import execute_evaluation_run
from app.evaluations.json_io import parse_strict_json_object, read_bounded_regular_text
from app.evaluations.metrics import build_evaluation_report
from app.evaluations.models import (
    CodegenCandidateIdentity,
    EvaluationRun,
    RiskLevel,
    RolloutStage,
)
from app.evaluations.publication import (
    build_publication_bundle,
    load_rollout_policy,
)
from app.evaluations.segments import build_segmented_report
from app.evaluations.subprocess_executor import SubprocessEvaluationExecutor
from app.evaluations.rollout import decide_rollout


MAX_EVALUATION_RUN_BYTES = 16 * 1024 * 1024
DEFAULT_ROLLOUT_POLICY_PATH = Path(__file__).with_name("rollout_policy_v3.json")


def _write_artifact(path: Path | None, artifact) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_evaluation_run(path: Path) -> EvaluationRun:
    payload = parse_strict_json_object(
        read_bounded_regular_text(path, max_bytes=MAX_EVALUATION_RUN_BYTES)
    )
    return EvaluationRun.model_validate_json(
        json.dumps(payload, allow_nan=False, separators=(",", ":"))
    )


async def _execute(args, corpus):
    environment = {
        **os.environ,
        "CODEGEN_MODEL": args.model,
        "CODEGEN_REVISION": args.codegen_revision,
    }
    candidate_identity = None
    if args.executor:
        executor = SubprocessEvaluationExecutor(
            [args.executor, *args.executor_arg],
            timeout_seconds=args.timeout_seconds or 3000,
            max_output_bytes=args.max_output_bytes,
            environment=environment,
        )
    else:
        candidate_identity = CodegenCandidateIdentity.build(
            controller_image_id=args.controller_image_id,
            candidate_image_id=args.docker_image,
            codegen_revision=args.codegen_revision,
            behavior_configuration_sha256=(
                codegen_behavior_configuration_sha256(environment)
            ),
        )
        executor = DockerEvaluationExecutor(
            image=args.docker_image,
            docker_bin=args.docker_bin,
            timeout_seconds=args.timeout_seconds,
            max_output_bytes=args.max_output_bytes,
            environment=environment,
            network=args.evaluation_network,
        )
    return await execute_evaluation_run(
        corpus,
        stage=RolloutStage(args.stage),
        executor=executor,
        model=args.model,
        codegen_revision=args.codegen_revision,
        candidate_identity=candidate_identity,
        run_id=args.run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--results", type=Path, help="validate an existing EvaluationRun")
    mode.add_argument(
        "--executor",
        help="advanced: argv[0] for a custom offline/shadow executor",
    )
    parser.add_argument("--executor-arg", action="append", default=[])
    parser.add_argument(
        "--docker-image",
        default=os.getenv("CODEGEN_SANDBOX_IMAGE", "apdl-codegen-sandbox:latest"),
        help="candidate image used by the default isolated Docker executor",
    )
    parser.add_argument(
        "--controller-image-id",
        help=(
            "immutable sha256 ID of the sealed controller running this evaluation"
        ),
    )
    parser.add_argument(
        "--docker-bin",
        default=os.getenv("CODEGEN_DOCKER_BIN", "docker"),
    )
    parser.add_argument(
        "--evaluation-network",
        default=os.getenv("CODEGEN_EVALUATION_NETWORK", ""),
        help="optional operator-managed network for model-provider egress",
    )
    parser.add_argument(
        "--stage",
        choices=[RolloutStage.offline.value, RolloutStage.shadow.value],
        default=RolloutStage.offline.value,
    )
    parser.add_argument("--model")
    parser.add_argument("--codegen-revision")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=None,
        help="per-case whole-pipeline timeout (default: production job budget)",
    )
    parser.add_argument("--max-output-bytes", type=int, default=1_000_000)
    parser.add_argument("--rollout-policy", type=Path)
    parser.add_argument("--run-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--segmented-output", type=Path)
    parser.add_argument("--bundle-output", type=Path)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if args.executor and args.bundle_output is not None:
        parser.error(
            "--bundle-output requires the default isolated Docker executor; "
            "custom executors are validation-only"
        )
    corpus = load_corpus(args.corpus)
    payload: dict = {"corpus_id": corpus.corpus_id, "cases": len(corpus.cases)}

    requested_execution = any(
        value is not None
        for value in (
            args.model,
            args.codegen_revision,
            args.controller_image_id,
            args.run_output,
            args.report_output,
            args.segmented_output,
            args.bundle_output,
            args.rollout_policy,
        )
    )
    if args.results:
        if args.bundle_output is not None or args.rollout_policy is not None:
            parser.error(
                "--results is validation-only and cannot create publication evidence"
            )
        run = load_evaluation_run(args.results)
        report = build_evaluation_report(run)
        segmented = build_segmented_report(run, corpus)
        payload.update(
            {
                "run": run.model_dump(mode="json"),
                "report": report.model_dump(mode="json"),
                "segmented_report": segmented.model_dump(mode="json"),
            }
        )
    elif args.executor or requested_execution:
        if not args.model or not args.codegen_revision:
            parser.error(
                "--model and --codegen-revision are required for evaluation execution"
            )
        if not args.executor and not args.controller_image_id:
            parser.error(
                "--controller-image-id is required for the default Docker evaluation"
            )
        if args.executor_arg and not args.executor:
            parser.error("--executor-arg requires --executor")
        completed = asyncio.run(_execute(args, corpus))
        run = completed.run
        report = completed.report
        segmented = completed.segmented_report
        payload["completed_evaluation"] = completed.model_dump(mode="json")
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    _write_artifact(args.run_output, run)
    _write_artifact(args.report_output, report)
    _write_artifact(args.segmented_output, segmented)
    if args.rollout_policy is not None or args.bundle_output is not None:
        policy_path = args.rollout_policy or DEFAULT_ROLLOUT_POLICY_PATH
        policy = load_rollout_policy(policy_path)
        decision = decide_rollout(
            requested_stage=RolloutStage.reviewed_pr,
            risk=RiskLevel.high,
            summary=report.summary,
            policy=policy,
        )
        payload["reviewed_pr_decision"] = decision.model_dump(mode="json")
        if args.bundle_output is not None and not decision.allowed:
            reasons = "; ".join(decision.reasons)
            parser.exit(
                status=1,
                message=(
                    "evaluation did not authorize reviewed_pr; reports were "
                    f"written but no publication bundle was created: {reasons}\n"
                ),
            )
        if args.bundle_output is not None:
            bundle = build_publication_bundle(report, policy)
            payload["publication_bundle"] = bundle.model_dump(mode="json")
            _write_artifact(args.bundle_output, bundle)

    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
