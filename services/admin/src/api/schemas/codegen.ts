// Codegen-service mirrors (services/codegen/app/models/changeset.py). The
// changeset status is a plain string on the wire; the UI maps known values.
import { z } from 'zod'

// One LLM prompt the run actually sent (brief compilation, an edit instruction
// handed to the coding agent, or a pre-push diff review). `system` is null for
// the edit stage: Aider supplies its own built-in system prompt there.
export const changesetPromptSchema = z
  .object({
    stage: z.string(),
    label: z.string(),
    system: z.string().nullable(),
    user: z.string(),
    notes: z.string().nullable(),
  })
  .strict()

export const taskSpecSchema = z
  .object({
    title: z.string(),
    spec: z.string(),
    context: z.record(z.unknown()),
    constraints: z.array(z.string()),
  })
  .strict()

export const KNOWN_CHANGESET_STATUSES = [
  'queued',
  'cloning',
  'editing',
  'testing',
  'tests_failed',
  'pushing',
  'pr_open',
  'ci_running',
  'ci_failed',
  'ci_passed',
  'unverified_external_ci',
  'waiting_approval',
  'merged',
  'abandoned',
  'error',
] as const

export const TERMINAL_CHANGESET_STATUSES = new Set<string>([
  'tests_failed',
  'merged',
  'abandoned',
  'error',
])

// Failed outcomes the codegen service will re-run via POST /{id}/retry (mirrors
// RETRYABLE_STATUSES in services/codegen/app/models/changeset.py). A retry
// enqueues a fresh changeset carrying the same task; `merged` rolls back with
// /revert instead, and in-flight statuses are still running.
export const RETRYABLE_CHANGESET_STATUSES = new Set<string>([
  'tests_failed',
  'ci_failed',
  'error',
  'abandoned',
])

export const changesetSchema = z
  .object({
    changeset_id: z.string(),
    project_id: z.string(),
    run_id: z.string().nullable(),
    task: taskSpecSchema,
    status: z.string(),
    base_branch: z.string().nullable(),
    branch: z.string().nullable(),
    pr_url: z.string().nullable(),
    pr_number: z.number().int().nullable(),
    pr_node_id: z.string().nullable(),
    ci_status: z
      .enum(['pending', 'passed', 'failed', 'unverified_external_ci'])
      .nullable(),
    // Stamped once at pr_open; anchors the CI sync's grace window. Null for
    // pre-PR changesets and rows predating the column.
    ci_awaiting_since: z.string().nullable(),
    ci_retry_count: z.number().int().nonnegative(),
    ci_remediation_status: z.enum(['idle', 'repairing', 'awaiting_ci', 'exhausted']),
    ci_failure_key: z.string().nullable(),
    ci_failure_summary: z.string().nullable(),
    merge_sha: z.string().nullable(),
    diff_stat: z.record(z.unknown()),
    prompts: z.array(changesetPromptSchema),
    error: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const changesetListSchema = z.array(changesetSchema)

// Repo connection registry (services/codegen/app/models/connection.py):
// binds a project to a GitHub App installation + repository.
export const REPO_SLUG_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

export const repoConnectionSchema = z
  .object({
    project_id: z.string(),
    installation_id: z.number().int(),
    repo: z.string(),
    default_base_branch: z.string(),
    policy: z.record(z.unknown()),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const repoConnectionCreateSchema = z
  .object({
    project_id: z.string().min(1),
    // Omitted → the codegen service resolves the live installation id from the
    // repo slug (and 422s cleanly if the App is not installed on it).
    installation_id: z.number().int().min(1).optional(),
    repo: z.string().regex(REPO_SLUG_PATTERN, 'Format: owner/name'),
    default_base_branch: z.string().min(1),
  })
  .strict()

// One repository the APDL GitHub App can reach (codegen GET /v1/github/repos —
// mirrors AccessibleRepo in services/codegen/app/github/installations.py).
export const accessibleRepoSchema = z
  .object({
    repo: z.string(),
    installation_id: z.number().int(),
    account: z.string(),
    default_branch: z.string(),
    private: z.boolean(),
  })
  .strict()

export const accessibleRepoListSchema = z.array(accessibleRepoSchema)
