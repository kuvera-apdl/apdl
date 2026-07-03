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

  it('validates the merge method', () => {
    expect(mergeRequestSchema.parse({ merge_method: 'squash' }).merge_method).toBe('squash')
    expect(mergeRequestSchema.safeParse({ merge_method: 'bad' }).success).toBe(false)
  })
})
