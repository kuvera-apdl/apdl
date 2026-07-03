// Codegen-service mirrors (services/codegen/app/models/changeset.py). The
// changeset status is a plain string on the wire; the UI maps known values.
import { z } from 'zod'

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
    diff_stat: z.record(z.unknown()),
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
    merge_sha: z.string().nullable(),
  .strict()
