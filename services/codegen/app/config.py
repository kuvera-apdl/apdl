"""Environment-derived configuration for the codegen service.

Mirrors the ``os.getenv`` convention used across APDL services (no settings
framework). Values are read at call time so tests can monkeypatch the
environment without re-importing the module.
"""

from __future__ import annotations

import base64
import os
import tempfile

from app.evaluations.models import RolloutStage

_DEFAULT_MODEL = "claude-opus-4-8"


def postgres_url() -> str:
    """DSN for the shared APDL PostgreSQL database."""
    return os.getenv("POSTGRES_URL", "postgresql://apdl:apdl_dev@localhost:5432/apdl")


# Local dev admin-console origins (Vite). Override in prod via CODEGEN_CORS_ORIGINS.
_DEFAULT_CORS_ORIGINS = ("http://localhost:5174", "http://localhost:5173")


def codegen_cors_origins() -> list[str]:
    """Explicit allow-list of browser origins permitted to call this service.

    This service opens/abandons PRs on customer repos, so it must NOT use
    wildcard CORS with credentials: Starlette would reflect any Origin and set
    Access-Control-Allow-Credentials, letting any site a victim visits issue
    credentialed cross-origin requests. Read a comma-separated CODEGEN_CORS_ORIGINS
    in prod; default to the local admin-console origins.
    """
    raw = os.getenv("CODEGEN_CORS_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    return origins or list(_DEFAULT_CORS_ORIGINS)


def internal_token() -> str:
    """Shared internal service token (``X-APDL-Internal-Token``).

    Empty in local dev, in which case the internal-token guard is permissive —
    matching the posture of the other services.
    """
    return os.getenv("APDL_INTERNAL_TOKEN", "")


def github_app_id() -> str:
    """The GitHub App's numeric ID (as a string)."""
    return os.getenv("GITHUB_APP_ID", "")


def github_app_private_key() -> str:
    r"""The GitHub App's PEM private key.

    Resolved so it works cleanly from a single-line ``.env`` (Docker) or a file
    (host), checked in this order:

    1. ``GITHUB_APP_PRIVATE_KEY`` — inline PEM. A one-line value whose newlines
       are backslash-escaped (``\n``) is restored to real newlines, so the key
       survives a ``.env`` file / compose interpolation.
    2. ``GITHUB_APP_PRIVATE_KEY_BASE64`` — base64 of the ``.pem``; the simplest
       single-line form to carry through ``.env`` (``base64 -w0 key.pem``).
    3. ``GITHUB_APP_PRIVATE_KEY_PATH`` — path to the ``.pem`` (``~`` expanded).
    """
    inline = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if inline.strip():
        # A one-line .env value often carries escaped newlines; restore them.
        if "\\n" in inline and "\n" not in inline:
            inline = inline.replace("\\n", "\n")
        return inline.strip()

    encoded = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""

    path = os.path.expanduser(os.getenv("GITHUB_APP_PRIVATE_KEY_PATH", ""))
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    return ""


def github_api_url() -> str:
    """Base URL for the GitHub REST API (override for GitHub Enterprise)."""
    return os.getenv("GITHUB_API_URL", "https://api.github.com")


def github_webhook_secret() -> str:
    """HMAC secret for verifying inbound GitHub webhooks. Empty = permissive dev."""
    return os.getenv("GITHUB_WEBHOOK_SECRET", "")


# --- Codegen editor configuration -----------------------------------------
# The in-process editor's knobs, read through getters (house style) rather than
# scattered ``os.getenv`` calls in ``AiderEditor.__init__``.


def codegen_model() -> str:
    """LiteLLM model id the editor drives (any provider key present in env)."""
    return os.getenv("CODEGEN_MODEL", _DEFAULT_MODEL)


def codegen_revision() -> str:
    """Immutable codegen candidate revision bound to rollout evidence.

    Production deployments should set ``CODEGEN_REVISION`` to the image or Git
    digest. The development fallback is intentionally conspicuous and can only
    publish if an operator explicitly authorizes that exact value.
    """
    return (
        os.getenv("CODEGEN_REVISION", "").strip()
        or os.getenv("GIT_COMMIT_SHA", "").strip()
        or "development-unversioned"
    )


def codegen_rollout_stage() -> RolloutStage:
    """Configured deployment stage; offline is the fail-closed default."""
    raw = os.getenv("CODEGEN_ROLLOUT_STAGE", RolloutStage.offline.value).strip()
    try:
        return RolloutStage(raw)
    except ValueError as exc:
        allowed = ", ".join(stage.value for stage in RolloutStage)
        raise ValueError(
            f"CODEGEN_ROLLOUT_STAGE must be one of: {allowed}"
        ) from exc


def codegen_rollout_authorization_path() -> str:
    """Operator-mounted rollout authorization artifact used for PR stages."""
    return os.getenv("CODEGEN_ROLLOUT_AUTHORIZATION_PATH", "").strip()


def codegen_aider_bin() -> str:
    """Path/name of the aider executable."""
    return os.getenv("CODEGEN_AIDER_BIN", "aider")


def codegen_cache_prompts() -> bool:
    """Enable Aider prompt caching (default on).

    Aider's `--cache-prompts` marks the static prefix (system prompt + repo map +
    read-only files) as cacheable across editing/review rounds — a large saving on a
    context-heavy editor like this. Harmless on models without cache support
    (Aider only applies it where the provider allows). Set to "false" to disable.
    """
    return os.getenv("CODEGEN_CACHE_PROMPTS", "true").lower() != "false"


def codegen_conventions_enabled() -> bool:
    """Pass the standing house-rules conventions file to the agent (default on).

    Loads ``app/editor/conventions.py`` as an Aider ``--read`` file so the edit
    bar is "wired in and exercised" rather than "builds green" (reachability,
    reuse the repo's SDK/primitives, test the new behavior). It joins the
    cacheable static prefix. Set to "false" to disable.
    """
    return os.getenv("CODEGEN_CONVENTIONS", "true").lower() != "false"


def codegen_sdk_reference_enabled() -> bool:
    """Return false until SDK guidance is generated for an exact lockfile version.

    Loads ``app/editor/sdk_reference.py`` as an Aider ``--read`` file, but only
    the reference for an SDK the repo actually depends on (``@apdl-oss/sdk`` for
    JS, ``apdl`` for Python). The SDK lives in ``node_modules`` / site-packages,
    which is outside Aider's repo map, so without this the agent cannot see the
    real ``track``/``identify`` call path and guesses (e.g. a ``window.apdl``
    global the SDK never reads). It joins the cacheable static prefix like the
    conventions. Static cross-version guidance is unsafe; an environment toggle
    cannot prove provenance, so Phase 0 disables this path unconditionally.
    """
    return False


def codegen_contracts_enabled() -> bool:
    """Resolve exact installed dependency contracts before editing (default on)."""
    return os.getenv("CODEGEN_CONTRACTS", "true").lower() != "false"


def codegen_contract_cache_dir() -> str:
    """Project-scoped content-addressed contract cache directory."""
    return os.getenv("CODEGEN_CONTRACT_CACHE_DIR") or os.path.join(
        tempfile.gettempdir(), "apdl-contract-cache"
    )


def codegen_contract_install_timeout() -> int:
    """Wall-clock limit for one frozen dependency installation."""
    return max(1, int(os.getenv("CODEGEN_CONTRACT_INSTALL_TIMEOUT", "600")))


def codegen_isolated_worker() -> bool:
    """Whether this process is the credential-minimal per-change worker."""
    return os.getenv("APDL_CODEGEN_ISOLATED_WORKER") == "true"


def codegen_helper_model() -> str:
    """LiteLLM model id for the auxiliary calls (brief compile + diff review).

    Defaults to the editing model so a single ``CODEGEN_MODEL`` configures the
    whole pipeline; override with ``CODEGEN_HELPER_MODEL`` to run the auxiliary
    steps on a cheaper/faster model than the editor.
    """
    return os.getenv("CODEGEN_HELPER_MODEL") or codegen_model()


def codegen_llm_timeout() -> float:
    """Per-auxiliary-LLM-call timeout, seconds (brief compile / diff review)."""
    return float(os.getenv("CODEGEN_LLM_TIMEOUT", "240"))


def codegen_brief_enabled() -> bool:
    """Compile the task spec into a repo-grounded engineering brief (default on).

    Approved feature proposals arrive written at product altitude — they can
    reference organizational actions and infrastructure the connected repo does
    not have. A pre-edit LLM pass translates the spec into a work order grounded
    in the actual repo (concrete files, explicit descoping of non-repo asks,
    checkable acceptance criteria) before the editing agent sees it. Skipped
    silently when LiteLLM or the provider key is absent. Set "false" to hand the
    raw spec to the editor unchanged.
    """
    return os.getenv("CODEGEN_BRIEF", "true").lower() != "false"


def codegen_review_enabled() -> bool:
    """Review the produced diff against the spec before pushing (default on).

    The verification command only proves the change *builds*; it happily passes
    a two-line diff that implements none of the spec (the observed nav-link-to-a-
    404 failure). A post-edit LLM review judges whether the diff plausibly
    delivers the spec's repo-implementable core and that new surfaces are
    reachable; a rejection feeds one retry with the reviewer's instructions,
    then fails the changeset. Skipped silently when LiteLLM or the provider key
    is absent. Set "false" to disable.
    """
    return os.getenv("CODEGEN_REVIEW", "true").lower() != "false"


def codegen_edit_retries() -> int:
    """Extra editing rounds after a verification or review failure (default 1).

    A failed verify/review re-invokes the agent with the failure output appended
    instead of terminally failing the changeset — most first-round failures
    (a bad import, a skipped requirement) are fixable with the error in hand.
    Floor of 0 (fail on the first failure, the pre-existing behavior).
    """
    return max(0, int(os.getenv("CODEGEN_EDIT_RETRIES", "1")))


def codegen_workdir() -> str:
    """Base directory for throwaway changeset workdirs (defaults to the tempdir)."""
    return os.getenv("CODEGEN_WORKDIR") or tempfile.gettempdir()


def codegen_keep_workdir() -> bool:
    """Keep the workdir after a run (for debugging) instead of deleting it."""
    return os.getenv("CODEGEN_KEEP_WORKDIR") == "true"


def codegen_git_timeout() -> int:
    """Per-``git``-invocation timeout, seconds."""
    return int(os.getenv("CODEGEN_GIT_TIMEOUT", "300"))


def codegen_agent_timeout() -> int:
    """Editing-agent (aider) timeout, seconds — also the per-job pipeline budget."""
    return int(os.getenv("CODEGEN_TIMEOUT", "1800"))


def codegen_job_budget() -> int:
    """Wall-clock budget for one FULL changeset pipeline, seconds.

    The agent timeout (``CODEGEN_TIMEOUT``) bounds a *single* aider invocation;
    a whole job is clone + (1 + retries) × aider + push. This derived
    budget is what must bound anything wrapping the pipeline as a unit: the
    sandbox container's ``docker run`` (killing it at the bare agent timeout
    truncates legitimate retry rounds) and the stale-changeset sweep deadline.
    Override explicitly with ``CODEGEN_JOB_BUDGET`` if the derivation doesn't
    fit (e.g. a huge clone).
    """
    override = os.getenv("CODEGEN_JOB_BUDGET", "")
    if override.strip():
        return max(1, int(override))
    rounds = 1 + codegen_edit_retries()
    return rounds * codegen_agent_timeout() + 2 * codegen_git_timeout()


def codegen_stale_sweep_interval() -> int:
    """Seconds between periodic stale-changeset sweeps (default 300; 0 disables).

    The sweep fails active-state changesets whose ``updated_at`` is older than
    ``2 × codegen_job_budget()`` — orphans of a crashed/restarted process that
    the startup sweep was too early to catch. See ``jobs.runner.run_stale_sweeper``.
    """
    return max(0, int(os.getenv("CODEGEN_STALE_SWEEP_INTERVAL", "300")))


def codegen_require_verify() -> bool:
    """Deprecated local-verification switch retained for config compatibility.

    APDL no longer executes repository tests as authoritative evidence; GitHub
    CI owns build, lint, test, and runtime verification.
    """
    return False


def codegen_max_concurrent_jobs() -> int:
    """Max changeset jobs allowed to run at once (default 1 — serialize).

    Each job runs a coding agent plus the repo's build/test, which is CPU- and
    memory-heavy; running several at once thrashes a small host. Jobs over the
    limit wait in ``queued`` until a slot frees. Floor of 1.
    """
    return max(1, int(os.getenv("CODEGEN_MAX_CONCURRENT_JOBS", "1")))


def codegen_ci_poll_interval() -> int:
    """Seconds between CI-status polls (default 60). Set ``0`` to disable.

    Polling is the zero-config trigger that advances open changesets without a
    public webhook endpoint. Disable it only when the GitHub webhook is wired and
    you want it to be the sole driver. Floor of 0; any positive value is honored.
    """
    return max(0, int(os.getenv("CODEGEN_CI_POLL_INTERVAL", "60")))


def codegen_ci_repair_retries() -> int:
    """Maximum same-PR repair commits after actionable GitHub CI failures."""
    return max(0, int(os.getenv("CODEGEN_CI_REPAIR_RETRIES", "2")))


def codegen_ci_repair_budget_seconds() -> int:
    """Maximum age of a failed head eligible for automated remediation."""
    return max(0, int(os.getenv("CODEGEN_CI_REPAIR_BUDGET_SECONDS", "3600")))
