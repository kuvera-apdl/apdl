"""Aider-backed Editor (plan decision D3, reworked — model-agnostic OSS agent).

Replaces the Claude Managed Agents editor. We now run the edit loop ourselves
with `Aider <https://github.com/Aider-AI/aider>`_, a git-native, model-agnostic
coding agent: it reaches any LiteLLM-supported model (OpenAI, Anthropic, Google,
local, …) chosen via ``CODEGEN_MODEL``, edits inside a clone, and iterates on test
failures reported by GitHub CI. The model is now a config choice, not a vendor
lock-in.

Execution model: the API defaults to a hardened, per-change container from
``Dockerfile.worker``. An in-process subprocess is available only for explicitly
trusted local repositories while publication is disabled. The real isolated path
still needs integration evidence with a model key and disposable live repository;
the deterministic boundary and argv/env construction are unit-tested.

Token custody (entirely ours now — there is no Anthropic git proxy): the GitHub
installation token is used only by the orchestrator for clone/push, injected into
git as a one-shot ``http.extraHeader`` via ``GIT_CONFIG_*`` environment variables.
Going through the env (not ``-c`` on the command line) keeps the header — and the
reversible base64 token inside it — off git's argv, so it can't leak through
``ps`` / ``/proc/<pid>/cmdline``; it is likewise never written to ``.git/config``.
It is never handed to Aider: that process runs with an isolated home, empty
service-owned configuration, and only the required model-provider credentials —
never the GitHub token or APDL service secrets. Repository-defined commands are
disabled; GitHub CI is the execution authority.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import config
from app.contracts.cache import FilesystemContractCache
from app.contracts.installer import (
    SandboxedCheckRunner,
    SandboxedInstallRunner,
    detect_contract_input_drift,
)
from app.contracts.models import ContractBundle, ContractRequest, RuntimeFingerprint
from app.contracts.render import render_contract_bundle
from app.contracts.resolver import resolve_contracts
from app.contracts.selection import select_contract_requests
from app.editor.base import EditRequest, EditResult
from app.editor.brief import (
    BRIEF_SYSTEM,
    build_brief_user,
    build_repo_digest,
    compile_brief,
)
from app.editor.conventions import CONVENTIONS_MD
from app.editor.llm import CompleteFn, resolve_completer
from app.inspection import (
    DependencySlice,
    InspectionSnapshot,
    RepositoryInspector,
    build_dependency_slice,
    render_dependency_slice,
    render_inspection_snapshot,
)
from app.profiling import profile_repository
from app.profiling.models import CommandKind, RepoProfile
from app.requirements import (
    bind_contract_evidence,
    compile_requirement_ledger,
    map_implementation_evidence,
    render_requirement_ledger,
)
from app.requirements.models import ImplementationStatus, RequirementLedger
from app.runtime.github_actions import (
    render_github_actions_workflow,
    workflow_content_is_apdl_owned,
)
from app.runtime.models import (
    GeneratedRuntimeWorkflowAttestation,
    RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
    RuntimeAcceptancePlan,
)
from app.runtime.planner import build_runtime_acceptance_plan
from app.runtime.render import render_runtime_acceptance_plan
from app.safety.gates import evaluate_pre_push
from app.safety.policy import VerifiedProtectedPathExemption
from app.semantic_review import (
    ModelResponseStatus,
    ReviewDecision,
    ReviewVerdict,
    SEMANTIC_REVIEW_SYSTEM,
    UncertaintyCode,
    assemble_review_verdict,
    build_deterministic_findings,
    build_deterministic_uncertainties,
    render_semantic_review_prompt,
)
from app.verification import (
    CoverageDisposition,
    VerificationCoverage,
    VerificationPlan,
    build_verification_plan,
    evaluate_verification_coverage,
    render_verification_coverage,
    render_verification_plan,
)

logger = logging.getLogger(__name__)

_DIFF_TEXT_CAP = 1_000_000  # cap the diff text fed to the secret scan (chars)
_ERR_TAIL = 800  # how much subprocess output to surface on a generic failure
# Verification/test failures are the actionable ``tests_failed`` case an operator
# reads to fix the change, so they get a much larger budget: a build/test log's
# real error (failing file, import, assertion, stack) needs room to survive.
_VERIFY_ERR_TAIL = 6000

_PROFILE_INPUT_NAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "uv.lock",
    "go.mod",
    "Cargo.toml",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "global.json",
}


def _profile_inputs_changed(paths: list[str]) -> bool:
    return any(
        path.startswith(".github/workflows/")
        or path.rsplit("/", 1)[-1] in _PROFILE_INPUT_NAMES
        or path.endswith((".csproj", ".fsproj", ".sln"))
        for path in paths
    )


def _tail(text: str, limit: int = _ERR_TAIL) -> str:
    """Return the last ``limit`` chars of ``text`` as a clean failure excerpt.

    Two fixes over a bare ``text[-limit:]`` so a surfaced error is actually
    informative: (1) the slice is snapped to the next line boundary so the
    excerpt never begins mid-line (which reads as corrupted — e.g. an import
    path shown as ``s/sdk/dist/...``), and (2) when content is dropped a marker
    naming how much was truncated is prepended, so a reader knows the head of the
    log is missing rather than assuming they have the whole story.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    clipped = text[-limit:]
    # Drop the partial leading line so the excerpt starts on a clean boundary.
    newline = clipped.find("\n")
    if 0 <= newline < len(clipped) - 1:
        clipped = clipped[newline + 1 :]
    dropped = len(text) - len(clipped)
    return f"[…truncated {dropped} leading chars of {len(text)}…]\n{clipped}"


# Env vars forwarded to the agent subprocess. LLM access only: the
# GitHub installation token and APDL service secrets (GITHUB_APP_PRIVATE_KEY,
# APDL_INTERNAL_TOKEN, POSTGRES_URL, …) are deliberately NOT in this allowlist.
_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "LANG",
    "LC_ALL",
    "OPENAI_API_KEY",
    "OPENAI_API_BASE",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "VERTEXAI_PROJECT",
    "VERTEXAI_LOCATION",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "COHERE_API_KEY",
    "TOGETHERAI_API_KEY",
    "FIREWORKS_API_KEY",
    "XAI_API_KEY",
    "OLLAMA_API_BASE",
    "AZURE_API_KEY",
    "AZURE_API_BASE",
    "AZURE_API_VERSION",
)

def _basic_auth_header(token: str) -> str:
    """GitHub App token → a one-shot Basic auth header value (never persisted)."""
    raw = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {raw}"


def _agent_env(home: Path) -> dict[str, str]:
    """Minimal agent environment with an isolated home and no repo configuration."""
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["AIDER_CONFIG_FILE"] = os.devnull
    env["AIDER_ENV_FILE"] = os.devnull
    env["AIDER_ANALYTICS"] = "false"  # headless: no phone-home / update prompts
    env["AIDER_CHECK_UPDATE"] = "false"
    return env


def _git_env() -> dict[str, str]:
    """Git environment: no credential prompts, no inherited app/LLM secrets."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _parse_numstat(numstat: str) -> dict[str, int]:
    """Parse ``git diff --numstat`` into a diff_stat dict.

    Returns ``{"files", "additions", "deletions"}``. Binary files render their
    counts as ``-``; they count toward ``files`` but contribute zero lines.
    """
    files = additions = deletions = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, _path = parts
        files += 1
        additions += int(added) if added.isdigit() else 0
        deletions += int(removed) if removed.isdigit() else 0
    return {"files": files, "additions": additions, "deletions": deletions}


def _capability_preamble(has_test_runner: bool, verify_cmd: str | None) -> str:
    """The per-repo 'testing reality' block prepended to the agent's message.

    Grounds the agent in what this specific repo can run, so it neither fabricates
    a test framework the repo lacks (which breaks the build) nor skips tests where
    a runner exists.
    """
    lines = ["## Repository verification context (read before writing code)"]
    if verify_cmd:
        lines.append(
            f"Your change is gated on this command passing: `{verify_cmd}`. It "
            "runs in GitHub CI as the authoritative type/build/test evidence. "
            "Make sure everything you add passes it."
        )
    else:
        lines.append(
            "No automated verification command was detected for this repo. Keep "
            "the change minimal and self-contained."
        )
    if has_test_runner:
        lines.append(
            "This repo HAS a test framework. Add a test that exercises the new "
            "behavior, using the framework the repo ALREADY depends on — never a "
            "different one."
        )
    else:
        lines.append(
            "This repo has NO test framework configured. Do NOT add test files and "
            "do NOT import a test library (vitest/jest/pytest/…): the dependency is "
            "absent, so the import will fail the build/type-check. Rely on the "
            "verification command above. Only add a runner if it is essential to "
            "the feature, and then add it to the manifest + lockfile in this change."
        )
    return "\n".join(lines)


@dataclass(frozen=True)
class _RepoProbe:
    """What the sandbox learned about a cloned repo before invoking the agent."""

    verify_cmd: str | None
    has_test_runner: bool
    preamble: str
    #: ``(filename, markdown)`` SDK references the repo's manifests call for.
    sdk_references: tuple[tuple[str, str], ...] = ()
    profile: RepoProfile | None = None


def _profile_verify_cmd(profile: RepoProfile) -> str | None:
    """Select one package's canonical GitHub-CI command chain as guidance."""
    for cwd in [".", *sorted({command.cwd for command in profile.commands})]:
        commands = [
            command.command
            for kind in (CommandKind.typecheck, CommandKind.build, CommandKind.test)
            for command in profile.commands
            if command.cwd == cwd and command.kind is kind
        ]
        if commands:
            return " && ".join(dict.fromkeys(commands))
    return None


def _probe_repo(repo_dir: Path, override_cmd: str | None) -> _RepoProbe:
    """Resolve the verification command + agent guidance for a cloned repo.

    ``override_cmd`` (the connection policy's ``test_cmd``) wins as the gate when
    set; the runner-presence signal still comes from the repo so the agent's
    guidance stays accurate.
    """
    profile = profile_repository(repo_dir)
    verify_cmd = override_cmd or _profile_verify_cmd(profile)
    has_runner = bool(profile.test_facilities) or any(
        command.kind is CommandKind.test for command in profile.commands
    )
    preamble = _capability_preamble(has_runner, verify_cmd)
    if profile.uncertainties:
        preamble += "\nRepository profiler uncertainties: " + ", ".join(
            sorted({uncertainty.code.value for uncertainty in profile.uncertainties})
        )
    return _RepoProbe(
        verify_cmd=verify_cmd,
        has_test_runner=has_runner,
        preamble=preamble,
        profile=profile,
    )


def _build_message(spec: str, constraints: list[str], preamble: str = "") -> str:
    """Compose the single headless instruction handed to Aider.

    ``preamble`` (the per-repo verification context) leads the message so the
    agent reads the repo's testing reality before the task itself.
    """
    message = spec.strip()
    if constraints:
        bullets = "\n".join(f"- {c}" for c in constraints)
        message = f"{message}\n\nConstraints:\n{bullets}"
    if preamble.strip():
        message = f"{preamble.strip()}\n\n{message}"
    return message


def _with_feedback(base_message: str, feedback: str) -> str:
    """Compose a retry message: the ORIGINAL work order plus the failure feedback.

    Each aider invocation is a fresh process with no chat history, so a retry
    that carried only the failure text would strip the agent of the task, the
    constraints, and the repo's testing reality — exactly the round most likely
    to do something desperate without them.
    """
    return f"{base_message}\n\n# Previous attempt — feedback to address first\n\n{feedback}"


def _verify_retry_message(test_cmd: str, output: str) -> str:
    """Follow-up agent feedback after the post-edit verification failed.

    The agent's commits are already in the clone, so a follow-up invocation sees
    its own work; the message carries the failing output so the fix is informed,
    and pins the intent so the "fix" is a repair, not a revert.
    """
    return (
        "Your previous change is committed in this repository but FAILED the "
        f"verification command: `{test_cmd}`.\n\n"
        f"Failing output (tail):\n```\n{_tail(output, _VERIFY_ERR_TAIL)}\n```\n\n"
        "Fix the failure while keeping the implemented feature intact — repair "
        "the code; do not revert the work to make the command pass."
    )


def _review_retry_message(verdict: ReviewVerdict) -> str:
    """Turn the strict evidence-backed verdict into actionable edit feedback."""
    problems = "\n".join(
        f"- {finding.message}" for finding in verdict.deterministic_findings
    )
    instructions = "\n".join(
        f"- {instruction}" for instruction in verdict.actionable_instructions
    )
    return (
        "An independent reviewer compared your committed change against the task "
        "spec and REJECTED it.\n\n"
        f"Problems found:\n{problems or '- (see instructions below)'}\n\n"
        f"Do this now:\n{instructions or '- Address every evidence-backed gap.'}"
    )


def _sole_external_ci_uncertainty(verdict: ReviewVerdict) -> bool:
    """Missing external CI may produce a draft, but no other uncertainty may hide."""
    return bool(verdict.uncertainties) and all(
        item.code is UncertaintyCode.verification_unverified
        for item in verdict.uncertainties
    )


def _model_settings_yaml(model: str) -> str:
    """Aider model-settings that disable ``temperature``.

    Newer models (e.g. ``claude-opus-4-8``) reject the ``temperature`` parameter,
    but aider sends it by default — which silently fails the request and produces
    a no-op edit. Disabling it is safe (the model uses its own default).
    """
    return f'- name: "{model}"\n  use_temperature: false\n'


def _contract_runtime() -> RuntimeFingerprint:
    versions = [f"python={platform.python_version()}"]
    for executable in ("node", "npm", "uv"):
        try:
            result = subprocess.run(
                [executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env={"PATH": os.environ.get("PATH", os.defpath)},
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode == 0 and result.stdout.strip():
            versions.append(f"{executable}={result.stdout.strip()}")
    return RuntimeFingerprint(
        runtime_name="apdl-codegen-worker-toolchains",
        runtime_version=";".join(versions),
        operating_system=platform.system().lower() or "unknown",
        architecture=platform.machine().lower() or "unknown",
    )


def _contract_requests_for_ledger(
    profile: RepoProfile, ledger: RequirementLedger
) -> list[ContractRequest]:
    """Select exact package names per stable requirement, merging duplicates."""
    selected: dict[tuple[str, str, str, str | None], ContractRequest] = {}
    for requirement in ledger.requirements:
        if requirement.implementation_status in {
            ImplementationStatus.blocked,
            ImplementationStatus.descoped,
        }:
            continue
        text = "\n".join(
            [
                requirement.original_source_text,
                requirement.observable_behavior,
                requirement.implementable_scope,
            ]
        )
        for request in select_contract_requests(
            profile, text, requirement_ids=[requirement.requirement_id]
        ):
            key = (
                request.ecosystem,
                request.package_path,
                request.package_name,
                request.exact_version,
            )
            previous = selected.get(key)
            if previous is None:
                selected[key] = request
                continue
            payload = previous.model_dump(mode="json")
            payload["requirement_ids"] = sorted(
                {*previous.requirement_ids, *request.requirement_ids}
            )
            selected[key] = ContractRequest.model_validate(payload)
    return [selected[key] for key in sorted(selected)]


_INSPECTION_STOPWORDS = frozenset(
    {
        "acceptance",
        "change",
        "existing",
        "implement",
        "requirement",
        "should",
        "tests",
        "that",
        "this",
        "with",
    }
)


def _inspection_for_ledger(
    repo_dir: Path, ledger: RequirementLedger
) -> InspectionSnapshot:
    """Build a safe inventory enriched with bounded task-focused searches."""
    inspector = RepositoryInspector(repo_dir)
    snapshot = inspector.snapshot()
    candidates: list[str] = []
    for requirement in ledger.requirements:
        for token in re.findall(
            r"[A-Za-z_][A-Za-z0-9_.:/-]{3,}", requirement.observable_behavior
        ):
            normalized = token.strip(".,:;()[]{}")
            if normalized.casefold() not in _INSPECTION_STOPWORDS:
                candidates.append(normalized)
    evidence = list(snapshot.evidence)
    for token in list(dict.fromkeys(candidates))[:12]:
        evidence.extend(
            inspector.search(
                token,
                case_sensitive=False,
                max_results=8,
            )
        )
    evidence = sorted(
        {item.evidence_id: item for item in evidence}.values(),
        key=lambda item: (item.path, item.start_line or 0, item.evidence_id),
    )
    return InspectionSnapshot(
        evidence=evidence,
        skipped_paths=snapshot.skipped_paths,
        bytes_inspected=snapshot.bytes_inspected,
        truncated=snapshot.truncated,
    )


def _coverage_retry_message(coverage: VerificationCoverage) -> str:
    return (
        "The deterministic risk policy rejected the current diff because required "
        "verification coverage is missing. Add tests using the repository's "
        "existing test framework for every medium/high-risk requirement. Do not "
        "skip or weaken existing checks. GitHub CI will execute the tests.\n\n"
        + render_verification_coverage(coverage)
    )


class AiderEditor:
    """Editor that drives Aider headlessly in a sandboxed clone (model-agnostic).

    The model is read from ``CODEGEN_MODEL`` (default ``claude-opus-4-8``); any
    LiteLLM-supported id works as long as the matching provider key is present in
    the service env (it is forwarded to the agent process, the GitHub token is
    not).
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        aider_bin: str | None = None,
        workdir_base: str | None = None,
        complete: CompleteFn | None = None,
    ) -> None:
        self._model = model or config.codegen_model()
        self._aider_bin = aider_bin or config.codegen_aider_bin()
        self._cache_prompts = config.codegen_cache_prompts()
        self._conventions = config.codegen_conventions_enabled()
        self._sdk_reference = config.codegen_sdk_reference_enabled()
        self._contracts_enabled = config.codegen_contracts_enabled()
        self._workdir_base = workdir_base or config.codegen_workdir()
        self._git_timeout = config.codegen_git_timeout()
        self._agent_timeout = config.codegen_agent_timeout()
        # Auxiliary LLM passes around the edit (brief compile + diff review).
        # ``complete`` is the injection seam for tests; production resolves a
        # LiteLLM-backed completer per run (None → the passes are skipped).
        self._complete = complete
        self._brief_enabled = config.codegen_brief_enabled()
        self._review_enabled = config.codegen_review_enabled()
        self._edit_retries = config.codegen_edit_retries()

    async def implement(self, request: EditRequest) -> EditResult:
        try:
            return await self._run(request)
        except Exception as exc:  # an attempt must never raise to the job runner
            logger.exception("Aider edit failed for %s", request.repo)
            return EditResult(success=False, branch=request.branch, error=str(exc))

    async def implement_workspace(
        self,
        request: EditRequest,
        workspace: Path,
    ) -> EditResult:
        """Run the production editing pipeline in an existing evaluation checkout.

        This is the credential-free execution seam used by the offline/shadow
        evaluator.  The caller owns a clean, already-materialized git workspace;
        this method edits that checkout in place and deliberately performs no
        clone, branch creation, fetch, or push.  Everything between repository
        preparation and publication remains identical to :meth:`implement`:
        profiling, requirement compilation, exact-contract resolution, the
        configured brief/Aider/review loop, verification coverage, and the full
        deterministic safety gate all still run.

        Evaluation workspaces cannot represent an existing-PR repair or remote
        revert because either operation requires GitHub state and credentials.
        Ordinary candidate failures follow the Editor contract and are returned
        as ``EditResult(success=False)`` rather than raised.
        """
        try:
            return await self._run(request, workspace=workspace)
        except Exception as exc:  # keep the same never-raise attempt contract
            # The evaluator's stderr crosses a trust boundary.  Do not log the
            # raw exception or candidate-controlled repository text here; the
            # typed result remains available to the in-process caller.
            logger.error("Aider workspace edit failed with an internal error")
            return EditResult(success=False, branch=request.branch, error=str(exc))

    async def _run(
        self,
        request: EditRequest,
        *,
        workspace: Path | None = None,
    ) -> EditResult:
        keep = config.codegen_keep_workdir()
        workspace_mode = workspace is not None
        if workspace_mode:
            assert workspace is not None
            repo_dir = workspace.expanduser().resolve()
            # Evaluation context/config files must remain outside the candidate
            # repository even if CODEGEN_WORKDIR was accidentally pointed at the
            # mounted checkout. Try the configured base first, then safe local
            # fallbacks, accepting only a directory outside the resolved repo.
            work: Path | None = None
            for candidate in (
                Path(self._workdir_base),
                Path(tempfile.gettempdir()),
                repo_dir.parent,
            ):
                scratch_base = candidate.expanduser().resolve()
                if scratch_base == repo_dir or repo_dir in scratch_base.parents:
                    continue
                try:
                    work = Path(
                        tempfile.mkdtemp(prefix="apdl-cs-", dir=scratch_base)
                    )
                except OSError:
                    continue
                break
            if work is None:
                raise RuntimeError(
                    "Workspace evaluation requires a writable scratch directory "
                    "outside the git worktree."
                )
        else:
            work = Path(tempfile.mkdtemp(prefix="apdl-cs-", dir=self._workdir_base))
            repo_dir = work / "repo"
        # A workspace evaluation never even derives an auth header, so an empty
        # token is a valid input and no credential can reach git accidentally.
        header = None if workspace_mode else _basic_auth_header(request.token)
        clone_url = None if workspace_mode else f"https://github.com/{request.repo}.git"
        # Prompt transcript for the operator UI (see EditResult.prompts). Every
        # exit path — fail() or success — carries whatever was recorded so far.
        prompts: list[dict[str, Any]] = []
        contract_bundle: ContractBundle | None = None
        requirement_ledger: RequirementLedger | None = request.requirement_ledger
        inspection_snapshot: InspectionSnapshot | None = request.inspection_snapshot
        dependency_slice: DependencySlice | None = request.dependency_slice
        verification_plan: VerificationPlan | None = request.verification_plan
        verification_coverage: VerificationCoverage | None = (
            request.verification_coverage
        )
        runtime_acceptance_plan: RuntimeAcceptancePlan | None = (
            request.runtime_acceptance_plan
        )
        generated_runtime_workflow: GeneratedRuntimeWorkflowAttestation | None = None
        review_verdict: ReviewVerdict | None = None
        verified_exemptions: tuple[VerifiedProtectedPathExemption, ...] = ()

        def fail(error: str) -> EditResult:
            return EditResult(
                success=False,
                branch=request.branch,
                error=error,
                prompts=prompts,
                contract_bundle=contract_bundle,
                requirement_ledger=requirement_ledger,
                inspection_snapshot=inspection_snapshot,
                dependency_slice=dependency_slice,
                verification_plan=verification_plan,
                verification_coverage=verification_coverage,
                runtime_acceptance_plan=runtime_acceptance_plan,
                generated_runtime_workflow=generated_runtime_workflow,
                review_verdict=review_verdict,
            )

        async def synchronize_runtime_workflow(
            profile: RepoProfile,
            plan: RuntimeAcceptancePlan,
        ) -> tuple[bool, str | None]:
            """Write only the effectively granted, APDL-owned workflow."""
            if not request.runtime_acceptance_policy.enabled or not plan.checks:
                return False, None
            workflow_relative = RUNTIME_ACCEPTANCE_WORKFLOW_PATH
            workflow_path = repo_dir / workflow_relative
            desired = render_github_actions_workflow(
                plan,
                profile,
                policy=request.runtime_acceptance_policy,
            )
            if workflow_path.exists():
                current = workflow_path.read_text(encoding="utf-8")
                if current == desired:
                    return False, None
                if not workflow_content_is_apdl_owned(current):
                    return (
                        False,
                        "runtime workflow generation refused: the reserved path "
                        f"{workflow_relative} already contains non-APDL-owned "
                        "content",
                    )
            workflow_path.parent.mkdir(parents=True, exist_ok=True)
            workflow_path.write_text(desired, encoding="utf-8")
            rc, out = await self._git(repo_dir, ["add", "--", workflow_relative])
            if rc != 0:
                return (
                    False,
                    "could not stage the authorized runtime workflow: "
                    + _tail(out),
                )
            rc, out = await self._git(
                repo_dir,
                ["commit", "-m", "chore(ci): add runtime acceptance evidence"],
            )
            if rc != 0:
                return (
                    False,
                    "could not commit the authorized runtime workflow: "
                    + _tail(out),
                )
            return True, None

        try:
            # 1. Production clones with one-shot auth.  Offline/shadow evaluation
            #    instead receives one clean, mutation-materialized checkout and
            #    must never gain a remote repository capability.
            if workspace_mode:
                if request.revert_sha or request.existing_branch or request.expected_head_sha:
                    return fail(
                        "Workspace evaluation does not support remote revert or "
                        "existing-branch repair inputs."
                    )
                if not repo_dir.is_dir():
                    return fail("Workspace evaluation requires an existing directory.")
                rc, out = await self._git(
                    repo_dir, ["rev-parse", "--is-inside-work-tree"]
                )
                if rc != 0 or out.strip() != "true":
                    return fail("Workspace evaluation requires a git worktree.")
                rc, out = await self._git(repo_dir, ["rev-parse", "--show-toplevel"])
                if rc != 0:
                    return fail("Workspace evaluation could not resolve its git root.")
                try:
                    git_root = Path(out.strip()).expanduser().resolve(strict=True)
                except (OSError, RuntimeError):
                    return fail("Workspace evaluation could not resolve its git root.")
                if git_root != repo_dir:
                    return fail(
                        "Workspace evaluation directory must be the git worktree root."
                    )
                rc, out = await self._git(
                    repo_dir, ["status", "--porcelain", "--untracked-files=all"]
                )
                if rc != 0:
                    return fail(
                        "Workspace evaluation could not inspect checkout state: "
                        + _tail(out)
                    )
                if out.strip():
                    return fail("Workspace evaluation requires a clean git worktree.")
            else:
                # Repairs clone the existing PR branch; initial runs clone base
                # and cut a new branch exactly as before.
                clone_branch = (
                    request.branch if request.existing_branch else request.base_branch
                )
                rc, out = await self._git(
                    None,
                    [
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        clone_branch,
                        clone_url,
                        str(repo_dir),
                    ],
                    auth_header=header,
                )
                if rc != 0:
                    return fail(f"clone failed: {_tail(out)}")
            rc, baseline = await self._git(repo_dir, ["rev-parse", "HEAD"])
            if rc != 0:
                return fail(f"could not resolve branch head: {_tail(baseline)}")
            baseline = baseline.strip()
            if request.expected_head_sha and baseline != request.expected_head_sha:
                return fail(
                    "Repair refused because the PR head changed from "
                    f"{request.expected_head_sha} to {baseline}."
                )
            if not workspace_mode and not request.existing_branch:
                rc, out = await self._git(repo_dir, ["checkout", "-b", request.branch])
                if rc != 0:
                    return fail(f"branch failed: {_tail(out)}")
            # Local commit identity so Aider's commits succeed without a global config.
            await self._git(repo_dir, ["config", "user.email", "codegen@apdl.dev"])
            await self._git(repo_dir, ["config", "user.name", "APDL Codegen"])

            # 2. Probe the repo: resolve the verification command (connection
            #    policy first, then detect) and the agent-facing testing reality.
            probe = _probe_repo(repo_dir, request.test_cmd)
            repo_profile = (probe.profile or profile_repository(repo_dir)).model_copy(
                update={"repo": request.repo, "branch": request.branch}
            )

            # 2b. Compile one strict, stable ledger before any model call. A
            # same-PR repair reuses the original ledger and IDs verbatim.
            if requirement_ledger is None:
                requirement_ledger = compile_requirement_ledger(
                    title=request.title,
                    spec=request.spec,
                    constraints=request.constraints,
                    risk=request.risk_level,
                    verification_command=probe.verify_cmd,
                )
            active_requirements = [
                item
                for item in requirement_ledger.requirements
                if item.implementation_status
                not in {ImplementationStatus.blocked, ImplementationStatus.descoped}
            ]
            if not active_requirements:
                return fail(
                    "Every requirement is explicitly blocked or descoped; no pull "
                    "request can be created."
                )

            inspection_snapshot = _inspection_for_ledger(
                repo_dir, requirement_ledger
            )
            verification_plan = build_verification_plan(
                requirement_ledger, repo_profile
            )
            runtime_acceptance_plan = build_runtime_acceptance_plan(
                repo_profile,
                verification_plan,
                policy=request.runtime_acceptance_policy,
            )
            workflow_changed, workflow_error = await synchronize_runtime_workflow(
                repo_profile, runtime_acceptance_plan
            )
            if workflow_error:
                return fail(workflow_error)
            if workflow_changed:
                # The generated file is now a repository fact. Rebind both plans
                # to the profile that GitHub will actually receive.
                repo_profile = profile_repository(repo_dir).model_copy(
                    update={"repo": request.repo, "branch": request.branch}
                )
                verification_plan = build_verification_plan(
                    requirement_ledger, repo_profile
                )
                runtime_acceptance_plan = build_runtime_acceptance_plan(
                    repo_profile,
                    verification_plan,
                    policy=request.runtime_acceptance_policy,
                )

            # 2c. Resolve every explicitly named direct dependency from an
            # isolated frozen install before the model sees an API claim. The
            # in-process API editor deliberately has no install authority, so
            # such work blocks unless it runs in the credential-minimal worker.
            if self._contracts_enabled:
                contract_requests = _contract_requests_for_ledger(
                    repo_profile, requirement_ledger
                )
                contract_bundle = await asyncio.to_thread(
                    resolve_contracts,
                    repo_dir,
                    project_scope=request.project_scope or request.repo,
                    repository=request.repo,
                    requests=contract_requests,
                    runtime=_contract_runtime(),
                    install_runner=SandboxedInstallRunner(
                        sandboxed=config.codegen_isolated_worker(),
                        timeout_seconds=config.codegen_contract_install_timeout(),
                        workdir_base=work,
                    ),
                    check_runner=SandboxedCheckRunner(
                        sandboxed=config.codegen_isolated_worker(),
                        workdir_base=work,
                    ),
                    cache=FilesystemContractCache(
                        Path(config.codegen_contract_cache_dir())
                    ),
                )
                blocked = [
                    resolution
                    for resolution in contract_bundle.resolutions
                    if resolution.disposition == "blocked"
                ]
                if blocked:
                    details = "; ".join(
                        f"{resolution.request.package_name}: "
                        + ", ".join(item.message for item in resolution.blockers)
                        for resolution in blocked
                    )
                    return fail(
                        "Exact dependency contract resolution blocked the change: "
                        + details
                    )
                requirement_ledger = bind_contract_evidence(
                    requirement_ledger, contract_bundle
                )

            # 3. Compile the spec into a repo-grounded engineering brief
            #    (auxiliary LLM pass; fail-open — an unusable brief means the raw
            #    spec runs, which is what would have happened anyway). The brief
            #    replaces the spec in the agent's message; the ORIGINAL spec
            #    stays the contract the post-edit review judges against. A
            #    deterministic revert needs no brief — the change is mechanical.
            need_brief = self._brief_enabled and request.revert_sha is None
            need_review = self._review_enabled and request.revert_sha is None
            fail_closed_auxiliary = request.risk_level in {"medium", "high"}
            complete = self._complete
            if complete is None and (need_brief or need_review):
                complete = resolve_completer()
            if (
                complete is None
                and fail_closed_auxiliary
                and (need_brief or need_review)
            ):
                return fail(
                    f"{request.risk_level}-risk change requires available brief/review "
                    "model gates; no completer is configured."
                )
            task_text = request.spec
            brief_used = False
            if need_brief and complete is not None:
                repo_digest = build_repo_digest(repo_dir, probe.profile)
                if contract_bundle and contract_bundle.resolutions:
                    repo_digest += "\n\n" + render_contract_bundle(contract_bundle)
                brief_prompt = {
                    "stage": "brief",
                    "label": "Brief compilation (spec → engineering brief)",
                    "system": BRIEF_SYSTEM,
                    "user": build_brief_user(
                        title=request.title,
                        spec=request.spec,
                        repo_digest=repo_digest,
                        verification_context=probe.preamble,
                    ),
                    "notes": None,
                }
                prompts.append(brief_prompt)
                brief = await compile_brief(
                    title=request.title,
                    spec=request.spec,
                    repo_digest=repo_digest,
                    verification_context=probe.preamble,
                    complete=complete,
                )
                if brief:
                    task_text = brief
                    brief_used = True
                else:
                    brief_prompt["notes"] = (
                        "Compilation produced no usable brief; the raw spec was "
                        "handed to the editing agent instead."
                    )
                    if fail_closed_auxiliary:
                        return fail(
                            f"{request.risk_level}-risk change requires a parseable "
                            "repository-grounded brief."
                        )

            # The optional prose brief may refine implementation detail, but it
            # cannot replace or weaken the canonical requirement contract.
            task_text = (
                f"{task_text.rstrip()}\n\n"
                f"{render_requirement_ledger(requirement_ledger)}\n\n"
                f"{render_verification_plan(verification_plan)}\n\n"
                f"{render_runtime_acceptance_plan(runtime_acceptance_plan, workflow_changes_authorized=request.runtime_acceptance_policy.enabled)}"
            )

            # 4. Run Aider headless. It edits + commits locally; it does NOT push.
            #    The settings file lives outside repo_dir so it never enters the diff.
            settings_file = work / "aider.model.settings.yml"
            settings_file.write_text(
                _model_settings_yaml(self._model), encoding="utf-8"
            )
            # A connected repository is untrusted input. Pin Aider to empty,
            # service-owned config/env files outside the clone and explicitly
            # disable every facility that can run repository-provided commands.
            aider_config = work / "aider.conf.yml"
            aider_config.write_text("{}\n", encoding="utf-8")
            aider_env = work / "aider.env"
            aider_env.write_text("", encoding="utf-8")
            argv = [
                self._aider_bin,
                "--config",
                str(aider_config),
                "--env-file",
                str(aider_env),
                "--model",
                self._model,
                "--model-settings-file",
                str(settings_file),
                "--yes-always",
                "--no-stream",
                "--no-pretty",
                "--no-auto-lint",
                "--no-auto-test",
                "--no-suggest-shell-commands",
                "--no-git-commit-verify",
                "--no-detect-urls",
                "--disable-playwright",
                "--no-notifications",
                "--no-watch-files",
                "--no-restore-chat-history",
                "--no-analytics",
                "--no-check-update",
            ]
            if self._conventions:
                # Standing house rules as a read-only context file (kept outside
                # repo_dir so it never enters the diff). It joins the cacheable
                # static prefix across editing rounds.
                conventions_file = work / "CONVENTIONS.md"
                conventions_file.write_text(CONVENTIONS_MD, encoding="utf-8")
                argv += ["--read", str(conventions_file)]
            if inspection_snapshot is not None:
                inspection_file = work / "INSPECTION.md"
                inspection_file.write_text(
                    render_inspection_snapshot(inspection_snapshot), encoding="utf-8"
                )
                argv += ["--read", str(inspection_file)]
            if verification_plan is not None:
                verification_file = work / "VERIFICATION_PLAN.md"
                verification_file.write_text(
                    render_verification_plan(verification_plan), encoding="utf-8"
                )
                argv += ["--read", str(verification_file)]
            if runtime_acceptance_plan is not None:
                runtime_file = work / "RUNTIME_ACCEPTANCE_PLAN.md"
                runtime_file.write_text(
                    render_runtime_acceptance_plan(
                        runtime_acceptance_plan,
                        workflow_changes_authorized=(
                            request.runtime_acceptance_policy.enabled
                        ),
                    ),
                    encoding="utf-8",
                )
                argv += ["--read", str(runtime_file)]
            if contract_bundle and contract_bundle.resolutions:
                contracts_file = work / "CONTRACTS.md"
                contracts_file.write_text(
                    render_contract_bundle(contract_bundle), encoding="utf-8"
                )
                argv += ["--read", str(contracts_file)]
            if self._sdk_reference:
                # Language-scoped SDK call-path reference(s) for whichever APDL
                # SDK the repo depends on. Written outside repo_dir so they never
                # enter the diff; they give the agent the real track()/identify()
                # path the SDK (in node_modules / site-packages) hides from the
                # repo map. Only refs for an SDK actually present are attached.
                for ref_name, ref_body in probe.sdk_references:
                    ref_file = work / ref_name
                    ref_file.write_text(ref_body, encoding="utf-8")
                    argv += ["--read", str(ref_file)]
            if self._cache_prompts:
                # Cache the static prefix (system + repo map) across edit rounds.
                argv.append("--cache-prompts")

            # 4b. A revert changeset is applied deterministically with
            #     ``git revert`` — the agent cannot see the merged commits (the
            #     clone is shallow) and must not reconstruct the revert from
            #     prose.
            agent_pending = True
            if request.revert_sha:
                assert header is not None  # workspace mode rejects remote reverts above
                revert_error = await self._revert_commit(
                    repo_dir, request.revert_sha, header
                )
                if revert_error:
                    return fail(revert_error)
                agent_pending = False

            # 5. The edit loop: aider → semantic review. Repository build/lint/
            #    tests execute in GitHub CI; APDL only supplies the discovered
            #    command as generation guidance. A review failure re-invokes the
            #    agent with feedback. The
            #    brief already embeds the verification context, so it is only
            #    prepended when the raw spec runs.
            initial_message = _build_message(
                task_text, request.constraints, "" if brief_used else probe.preamble
            )
            # What the agent reads besides the message, for the transcript: its
            # own built-in system prompt plus any --read context files above.
            context_files = [
                Path(argv[i + 1]).name for i, a in enumerate(argv) if a == "--read"
            ]
            edit_notes = (
                "The system prompt for this step is Aider's built-in editing "
                "prompt (not authored by APDL)."
            )
            if context_files:
                edit_notes += (
                    " Read-only context files attached: "
                    + ", ".join(context_files)
                    + "."
                )
            message = initial_message
            retries_left = self._edit_retries
            base = baseline
            out = ""
            edit_attempt = 0
            review_round = 0
            while True:
                if agent_pending:
                    edit_attempt += 1
                    prompts.append(
                        {
                            "stage": "edit",
                            "label": f"Edit instruction (attempt {edit_attempt})",
                            "system": None,
                            "user": message,
                            "notes": edit_notes,
                        }
                    )
                    rc, out = await self._exec(
                        [*argv, "--message", message],
                        cwd=repo_dir,
                        env=_agent_env(work / "agent-home"),
                        timeout=self._agent_timeout,
                    )
                    if rc != 0:
                        return fail(
                            f"aider exited {rc}: {_tail(out, _VERIFY_ERR_TAIL)}"
                        )
                agent_pending = True

                # Compute the diff for the review + pre-push gates. No diff → no PR.
                rc, names = await self._git(
                    repo_dir, ["diff", "--name-only", f"{base}..HEAD"]
                )
                changed_paths = [p for p in names.splitlines() if p.strip()]
                if rc != 0 or not changed_paths:
                    # Surface aider's own output so a no-op edit (e.g. an unreachable
                    # or misnamed model) is diagnosable from the changeset error.
                    return fail(
                        f"The agent produced no changes. aider: {_tail(out, _VERIFY_ERR_TAIL)}"
                    )
                _, diff_text = await self._git(repo_dir, ["diff", f"{base}..HEAD"])
                dependency_slice = build_dependency_slice(repo_dir, changed_paths)
                if _profile_inputs_changed(changed_paths):
                    repo_profile = profile_repository(repo_dir).model_copy(
                        update={"repo": request.repo, "branch": request.branch}
                    )
                    verification_plan = build_verification_plan(
                        requirement_ledger, repo_profile
                    )
                    runtime_acceptance_plan = build_runtime_acceptance_plan(
                        repo_profile,
                        verification_plan,
                        policy=request.runtime_acceptance_policy,
                    )
                    workflow_changed, workflow_error = (
                        await synchronize_runtime_workflow(
                            repo_profile, runtime_acceptance_plan
                        )
                    )
                    if workflow_error:
                        return fail(workflow_error)
                    if workflow_changed:
                        # A manifest/workflow edit can change the exact runtime
                        # command or artifact plan. Commit the regenerated file,
                        # then recompute every diff-derived evidence artifact.
                        repo_profile = profile_repository(repo_dir).model_copy(
                            update={"repo": request.repo, "branch": request.branch}
                        )
                        verification_plan = build_verification_plan(
                            requirement_ledger, repo_profile
                        )
                        runtime_acceptance_plan = build_runtime_acceptance_plan(
                            repo_profile,
                            verification_plan,
                            policy=request.runtime_acceptance_policy,
                        )
                        rc, names = await self._git(
                            repo_dir, ["diff", "--name-only", f"{base}..HEAD"]
                        )
                        if rc != 0:
                            return fail("could not inspect regenerated workflow diff")
                        changed_paths = [
                            path for path in names.splitlines() if path.strip()
                        ]
                        _, diff_text = await self._git(
                            repo_dir, ["diff", f"{base}..HEAD"]
                        )
                        dependency_slice = build_dependency_slice(
                            repo_dir, changed_paths
                        )
                verification_coverage = evaluate_verification_coverage(
                    verification_plan,
                    changed_paths=changed_paths,
                    policy_authorized_workflow_paths=(
                        [RUNTIME_ACCEPTANCE_WORKFLOW_PATH]
                        if request.runtime_acceptance_policy.enabled
                        and RUNTIME_ACCEPTANCE_WORKFLOW_PATH
                        in changed_paths
                        else []
                    ),
                )
                evidence_context = (
                    render_dependency_slice(dependency_slice)
                    + "\n\n"
                    + render_verification_coverage(verification_coverage)
                )
                if verification_coverage.disposition in {
                    CoverageDisposition.rejected_workflow_gate_relaxation,
                    CoverageDisposition.requires_protected_workflow_review,
                }:
                    return fail(verification_coverage.disposition_reason)
                if (
                    verification_coverage.disposition
                    is CoverageDisposition.missing_required_coverage
                ):
                    if retries_left > 0:
                        retries_left -= 1
                        message = _with_feedback(
                            initial_message,
                            _coverage_retry_message(verification_coverage),
                        )
                        logger.info(
                            "Risk policy requires additional tests for %s; retrying.",
                            request.repo,
                        )
                        continue
                    return fail(verification_coverage.disposition_reason)

                # Run deterministic semantic checks on every material diff, even
                # when the optional independent model call is disabled. The model
                # can add judgment but cannot override a deterministic error.
                # A mechanical revert remains outside this judgment boundary.
                if request.revert_sha is None:
                    contracts_for_review = contract_bundle or ContractBundle()
                    findings = build_deterministic_findings(
                        ledger=requirement_ledger,
                        contracts=contracts_for_review,
                        dependency_slice=dependency_slice,
                        verification_plan=verification_plan,
                        verification_coverage=verification_coverage,
                        diff_text=diff_text,
                    )
                    uncertainties = build_deterministic_uncertainties(
                        ledger=requirement_ledger,
                        contracts=contracts_for_review,
                        dependency_slice=dependency_slice,
                        verification_plan=verification_plan,
                        verification_coverage=verification_coverage,
                        diff_text=diff_text,
                    )
                    model_response: str | None = None
                    if need_review and complete is not None:
                        review_round += 1
                        review_prompt = render_semantic_review_prompt(
                            ledger=requirement_ledger,
                            contracts=contracts_for_review,
                            dependency_slice=dependency_slice,
                            verification_plan=verification_plan,
                            verification_coverage=verification_coverage,
                            deterministic_findings=findings,
                            deterministic_uncertainties=uncertainties,
                            diff_text=diff_text,
                        )
                        prompts.append(
                            {
                                "stage": "review",
                                "label": f"Semantic review (round {review_round})",
                                "system": SEMANTIC_REVIEW_SYSTEM,
                                "user": review_prompt,
                                "notes": (
                                    "Independent evidence context; GitHub CI has "
                                    "not reported at this stage."
                                ),
                            }
                        )
                        model_response = await complete(
                            SEMANTIC_REVIEW_SYSTEM, review_prompt
                        )
                    review_verdict = assemble_review_verdict(
                        ledger=requirement_ledger,
                        contracts=contracts_for_review,
                        dependency_slice=dependency_slice,
                        verification_plan=verification_plan,
                        verification_coverage=verification_coverage,
                        diff_text=diff_text,
                        model_response_text=model_response,
                    )

                    if review_verdict.overall_decision is ReviewDecision.rejected:
                        if retries_left > 0:
                            retries_left -= 1
                            message = _with_feedback(
                                initial_message + "\n\n" + evidence_context,
                                _review_retry_message(review_verdict),
                            )
                            logger.info(
                                "Semantic review rejected the change for %s; "
                                "retrying with evidence-backed instructions.",
                                request.repo,
                            )
                            continue
                        return fail(
                            "semantic review rejected the change: "
                            + "; ".join(review_verdict.actionable_instructions)
                        )

                    if review_verdict.overall_decision is ReviewDecision.unverified:
                        external_ci_only = (
                            verification_coverage.disposition
                            is CoverageDisposition.unverified_external_ci
                            and review_verdict.model_response_status
                            is ModelResponseStatus.parsed
                            and _sole_external_ci_uncertainty(review_verdict)
                        )
                        model_failed = review_verdict.model_response_status in {
                            ModelResponseStatus.unavailable,
                            ModelResponseStatus.invalid,
                        }
                        if external_ci_only:
                            logger.info(
                                "Semantic review for %s remains explicitly "
                                "unverified only because GitHub CI is unavailable.",
                                request.repo,
                            )
                        elif model_failed:
                            if fail_closed_auxiliary:
                                return fail(
                                    f"{request.risk_level}-risk change requires a "
                                    "valid independent semantic-review verdict."
                                )
                        elif retries_left > 0:
                            retries_left -= 1
                            message = _with_feedback(
                                initial_message + "\n\n" + evidence_context,
                                _review_retry_message(review_verdict),
                            )
                            continue
                        elif fail_closed_auxiliary:
                            return fail(
                                "semantic review could not verify the change: "
                                + "; ".join(review_verdict.actionable_instructions)
                            )
                break

            generated_path = (
                RUNTIME_ACCEPTANCE_WORKFLOW_PATH
                if request.runtime_acceptance_policy.enabled
                else None
            )
            material_changed_paths = [
                path for path in changed_paths if path != generated_path
            ]
            if request.revert_sha is None and not material_changed_paths:
                return fail(
                    "The agent produced no material implementation change; the "
                    "generated runtime workflow alone cannot satisfy the task."
                )
            requirement_ledger = map_implementation_evidence(
                requirement_ledger, material_changed_paths or changed_paths
            )
            if not requirement_ledger.ready_for_pull_request():
                return fail(
                    "Requirement ledger is incomplete; every active criterion must "
                    "map to changed or confirmed-existing code before PR creation."
                )

            _, numstat = await self._git(
                repo_dir, ["diff", "--numstat", f"{base}..HEAD"]
            )
            diff_stat = _parse_numstat(numstat)

            # Contract evidence is valid only for the exact manifest/lockfile
            # hashes it was compiled from. A dependency edit must be resolved in
            # a fresh attempt; stale API evidence is never pushed.
            if contract_bundle is not None:
                drift = [
                    item
                    for resolution in contract_bundle.resolutions
                    if (item := detect_contract_input_drift(repo_dir, resolution))
                    is not None
                ]
                if drift:
                    rendered = "; ".join(
                        f"{item.package_name}: {', '.join(item.changed_paths)}"
                        for item in drift
                    )
                    return fail(
                        "Dependency manifest/lockfile changed after contract "
                        f"resolution; evidence invalidated: {rendered}"
                    )

            if (
                request.runtime_acceptance_policy.enabled
                and runtime_acceptance_plan is not None
                and runtime_acceptance_plan.checks
            ):
                workflow_path = (
                    repo_dir
                    / RUNTIME_ACCEPTANCE_WORKFLOW_PATH
                )
                expected_workflow = render_github_actions_workflow(
                    runtime_acceptance_plan,
                    repo_profile,
                    policy=request.runtime_acceptance_policy,
                )
                if (
                    not workflow_path.is_file()
                    or workflow_path.read_text(encoding="utf-8")
                    != expected_workflow
                ):
                    return fail(
                        "The policy-authorized runtime workflow does not match "
                        "the deterministic renderer; branch NOT pushed."
                    )
                generated_runtime_workflow = GeneratedRuntimeWorkflowAttestation(
                    path=RUNTIME_ACCEPTANCE_WORKFLOW_PATH,
                    content_sha256=hashlib.sha256(
                        expected_workflow.encode("utf-8")
                    ).hexdigest(),
                    runtime_acceptance_plan_sha256=(
                        runtime_acceptance_plan.evidence_hash()
                    ),
                )
                verified_exemptions = (
                    VerifiedProtectedPathExemption(
                        content_sha256=generated_runtime_workflow.content_sha256,
                        runtime_acceptance_plan_sha256=(
                            generated_runtime_workflow.runtime_acceptance_plan_sha256
                        ),
                    ),
                )

            # Deterministic pre-push gates on the FULL diff, before anything
            # reaches the remote: a secret-bearing or protected-path change must
            # never land on GitHub, not merely be denied a PR. (The job runner
            # re-checks the same gates as a backstop before opening the PR.)
            gate = evaluate_pre_push(
                diff_stat=diff_stat,
                changed_paths=changed_paths,
                diff_text=diff_text,
                policy=request.safety_policy,
                verified_exemptions=verified_exemptions,
            )
            if not gate.passed:
                return fail(
                    "pre-push gate failed; branch NOT pushed: "
                    + "; ".join(gate.violations)
                )

            # 6. Production publishes the gated branch.  Evaluation deliberately
            #    leaves the resulting commit only in its materialized workspace;
            #    the sealed outer harness inspects that tree after this returns.
            if not workspace_mode:
                push_args = ["push"]
                if request.expected_head_sha:
                    push_args.append(
                        "--force-with-lease="
                        f"refs/heads/{request.branch}:{request.expected_head_sha}"
                    )
                push_args += ["origin", f"{request.branch}:{request.branch}"]
                assert header is not None
                rc, out = await self._git(
                    repo_dir,
                    push_args,
                    auth_header=header,
                )
                if rc != 0:
                    return fail(f"push failed: {_tail(out)}")
            rc, head_sha = await self._git(repo_dir, ["rev-parse", "HEAD"])
            if rc != 0:
                label = "workspace" if workspace_mode else "pushed"
                return fail(f"could not resolve {label} head: {_tail(head_sha)}")

            return EditResult(
                success=True,
                branch=request.branch,
                diff_stat=diff_stat,
                changed_paths=changed_paths,
                diff_text=diff_text[:_DIFF_TEXT_CAP],
                prompts=prompts,
                head_sha=head_sha.strip(),
                contract_bundle=contract_bundle,
                requirement_ledger=requirement_ledger,
                inspection_snapshot=inspection_snapshot,
                dependency_slice=dependency_slice,
                verification_plan=verification_plan,
                verification_coverage=verification_coverage,
                runtime_acceptance_plan=runtime_acceptance_plan,
                generated_runtime_workflow=generated_runtime_workflow,
                review_verdict=review_verdict,
            )
        finally:
            if not keep:
                shutil.rmtree(work, ignore_errors=True)

    async def _git(
        self, cwd: Path | None, args: list[str], *, auth_header: str | None = None
    ) -> tuple[int, str]:
        env = _git_env()
        if auth_header is not None:
            # Inject the one-shot ``http.extraHeader`` via GIT_CONFIG_* env vars
            # instead of ``-c`` on the command line, so the (reversible base64)
            # token never lands on git's argv where ps / /proc would expose it.
            env = {
                **env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": auth_header,
            }
        argv = ["git"]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += args
        return await self._exec(argv, cwd=None, env=env, timeout=self._git_timeout)

    async def _revert_commit(
        self, repo_dir: Path, sha: str, auth_header: str
    ) -> str | None:
        """Apply ``git revert`` of ``sha`` in the clone; return an error or None.

        The clone is shallow (depth 1 of the base branch), so the revert target
        and its parent are fetched first — GitHub serves reachable SHAs directly.
        A conflicting revert is aborted and surfaced rather than handed to the
        agent on top of a conflicted tree.
        """
        rc, out = await self._git(
            repo_dir,
            ["fetch", "--depth", "2", "origin", sha],
            auth_header=auth_header,
        )
        if rc != 0:
            return f"could not fetch revert target {sha}: {_tail(out)}"
        rc, out = await self._git(repo_dir, ["rev-list", "--parents", "-n", "1", sha])
        if rc != 0:
            return f"could not inspect revert target {sha}: {_tail(out)}"
        revert = ["revert", "--no-edit"]
        if len(out.split()) > 2:  # merge commit → revert against mainline parent 1
            revert += ["-m", "1"]
        rc, out = await self._git(repo_dir, [*revert, sha])
        if rc != 0:
            await self._git(repo_dir, ["revert", "--abort"])
            return (
                f"git revert of {sha} conflicts with later changes on the base "
                f"branch; revert it manually. {_tail(out)}"
            )
        return None

    async def _exec(
        self, argv: list[str], *, cwd: Path | None, env: dict[str, str], timeout: int
    ) -> tuple[int, str]:
        """Run a subprocess; return (returncode, combined output).

        A non-zero exit is data, not an error — only spawn faults or a timeout
        surface as exceptions (caught by :meth:`implement`) or a synthetic 124.
        """
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"timed out after {timeout}s"
        return proc.returncode or 0, (stdout or b"").decode("utf-8", "replace")
