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
    ci_status: z.string().nullable(),
    merge_sha: z.string().nullable(),
    diff_stat: z.record(z.unknown()),
    prompts: z.array(changesetPromptSchema),
    error: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const changesetListSchema = z.array(changesetSchema)

export const mergeRequestSchema = z
  .object({
    merge_method: z.enum(['squash', 'merge', 'rebase']),
  })
  .strict()

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
    installation_id: z.number().int().min(1),
    repo: z.string().regex(REPO_SLUG_PATTERN, 'Format: owner/name'),
    default_base_branch: z.string().min(1),
  })
  .strict()
