"""Execute or validate credential-minimal offline/shadow evaluation artifacts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from app.evaluations.corpus import DEFAULT_CORPUS_PATH, load_corpus
from app.evaluations.execution import execute_evaluation_run
from app.evaluations.json_io import parse_strict_json_object, read_bounded_regular_text
from app.evaluations.metrics import build_evaluation_report
from app.evaluations.models import EvaluationRun, RolloutStage
from app.evaluations.publication import (
    build_publication_bundle,
    load_rollout_policy,
)
from app.evaluations.segments import build_segmented_report
from app.evaluations.subprocess_executor import SubprocessEvaluationExecutor


MAX_EVALUATION_RUN_BYTES = 16 * 1024 * 1024


def _write_artifact(path: Path | None, artifact) -> None:
    if path is not None:
        path.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_evaluation_run(path: Path) -> EvaluationRun:
    return EvaluationRun.model_validate(
        parse_strict_json_object(
            read_bounded_regular_text(path, max_bytes=MAX_EVALUATION_RUN_BYTES)
        )
    )


async def _execute(args, corpus):
    environment = {**os.environ, "CODEGEN_MODEL": args.model}
    executor = SubprocessEvaluationExecutor(
        [args.executor, *args.executor_arg],
        timeout_seconds=args.timeout_seconds,
        max_output_bytes=args.max_output_bytes,
        environment=environment,
    )
    return await execute_evaluation_run(
        corpus,
        stage=RolloutStage(args.stage),
        executor=executor,
        model=args.model,
        codegen_revision=args.codegen_revision,
        run_id=args.run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--results", type=Path, help="validate an existing EvaluationRun")
    mode.add_argument("--executor", help="argv[0] for an offline/shadow executor")
    parser.add_argument("--executor-arg", action="append", default=[])
    parser.add_argument(
        "--stage",
        choices=[RolloutStage.offline.value, RolloutStage.shadow.value],
        default=RolloutStage.offline.value,
    )
    parser.add_argument("--model")
    parser.add_argument("--codegen-revision")
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-seconds", type=float, default=300)
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
    if args.bundle_output is not None and args.rollout_policy is None:
        parser.error("--bundle-output requires an explicit --rollout-policy")
    corpus = load_corpus(args.corpus)
    payload: dict = {"corpus_id": corpus.corpus_id, "cases": len(corpus.cases)}

    if args.executor:
        if not args.model or not args.codegen_revision:
            parser.error("--model and --codegen-revision are required with --executor")
        completed = asyncio.run(_execute(args, corpus))
        run = completed.run
        report = completed.report
        segmented = completed.segmented_report
        payload["completed_evaluation"] = completed.model_dump(mode="json")
    elif args.results:
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
    else:
        if any(
            path is not None
            for path in (
                args.run_output,
                args.report_output,
                args.segmented_output,
                args.bundle_output,
                args.rollout_policy,
            )
        ):
            parser.error("artifact outputs require --executor or --results")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    _write_artifact(args.run_output, run)
    _write_artifact(args.report_output, report)
    _write_artifact(args.segmented_output, segmented)
    if args.rollout_policy is not None:
        policy = load_rollout_policy(args.rollout_policy)
        bundle = build_publication_bundle(report, policy)
        payload["publication_bundle"] = bundle.model_dump(mode="json")
        _write_artifact(args.bundle_output, bundle)

    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
