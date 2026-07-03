import { describe, expect, it } from 'vitest'

import {
  changesetListSchema,
  changesetSchema,
  mergeRequestSchema,
} from '@/api/schemas/codegen'

const sample = {
  changeset_id: 'cs_1',
  project_id: 'demo',
  run_id: null,
  task: { title: 'Add dark mode', spec: 'Implement a toggle.', context: {}, constraints: [] },
  status: 'pr_open',
  base_branch: 'main',
  branch: 'apdl/add-dark-mode-cs_1',
  pr_url: 'https://github.com/acme/widgets/pull/1',
  pr_number: 1,
  pr_node_id: 'PR_1',
  ci_status: 'passed',
  merge_sha: null,
  diff_stat: { files: 2, additions: 30 },
  prompts: [
    {
      stage: 'edit',
      label: 'Edit instruction (attempt 1)',
      system: null,
      user: 'Implement a toggle.',
      notes: "The system prompt for this step is Aider's built-in editing prompt (not authored by APDL).",
    },
  ],
  error: null,
  created_at: '2026-06-17T12:00:00Z',
  updated_at: '2026-06-17T12:05:00Z',
}

describe('codegen schemas', () => {
  it('parses a changeset', () => {
    expect(changesetSchema.parse(sample).changeset_id).toBe('cs_1')
  })

  it('parses a changeset list', () => {
    expect(changesetListSchema.parse([sample])).toHaveLength(1)
  })

  it('rejects unknown fields (strict)', () => {
    expect(changesetSchema.safeParse({ ...sample, extra: true }).success).toBe(false)
  })

  it('parses the prompt transcript', () => {
    const parsed = changesetSchema.parse(sample)
    expect(parsed.prompts).toHaveLength(1)
    expect(parsed.prompts[0].stage).toBe('edit')
    expect(parsed.prompts[0].system).toBeNull()
  })

  it('rejects unknown prompt fields (strict)', () => {
    const bad = { ...sample, prompts: [{ ...sample.prompts[0], extra: true }] }
    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('validates the merge method', () => {
    expect(mergeRequestSchema.parse({ merge_method: 'squash' }).merge_method).toBe('squash')
    expect(mergeRequestSchema.safeParse({ merge_method: 'bad' }).success).toBe(false)
  })
})
