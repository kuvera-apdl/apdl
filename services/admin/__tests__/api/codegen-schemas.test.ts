import { describe, expect, it } from 'vitest'

import {
  changesetListSchema,
  changesetSchema,
} from '@/api/schemas/codegen'
import {
  makeReviewVerdict,
  makeRuntimeAcceptancePlan,
  makeRuntimeEvidenceAssessment,
  makeVerificationCoverage,
  makeVerificationPlan,
} from '../helpers/fixtures'

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
  head_sha: 'a'.repeat(40),
  github_pr_status: 'open',
  external_ci_status: 'passed',
  external_ci_awaiting_since: '2026-06-17T12:03:00Z',
  ci_retry_count: 0,
  ci_remediation_status: 'idle',
  ci_failure_key: null,
  ci_failure_summary: null,
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
  contract_bundle: null,
  requirement_ledger: null,
  inspection_snapshot: null,
  dependency_slice: null,
  verification_plan: null,
  verification_coverage: null,
  runtime_acceptance_plan: null,
  runtime_evidence_assessment: null,
  review_verdict: null,
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

  it('rejects CI or review values as lifecycle statuses', () => {
    expect(changesetSchema.safeParse({ ...sample, status: 'ci_passed' }).success).toBe(false)
    expect(changesetSchema.safeParse({ ...sample, status: 'tests_failed' }).success).toBe(false)
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

  it('parses strict verification planning and coverage evidence', () => {
    const parsed = changesetSchema.parse({
      ...sample,
      verification_plan: makeVerificationPlan(),
      verification_coverage: makeVerificationCoverage(),
    })

    expect(parsed.verification_plan?.authority).toBe('github_ci')
    expect(parsed.verification_coverage?.disposition).toBe('ready_for_github_ci')
  })

  it('rejects verification evidence that claims APDL was authoritative', () => {
    const bad = {
      ...sample,
      verification_coverage: {
        ...makeVerificationCoverage(),
        apdl_declared_verified: true,
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('rejects unknown verification plan fields (strict)', () => {
    const bad = {
      ...sample,
      verification_plan: {
        ...makeVerificationPlan(),
        extra: true,
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('parses strict runtime evidence without replacing the GitHub-owned CI projection', () => {
    const parsed = changesetSchema.parse({
      ...sample,
      runtime_acceptance_plan: makeRuntimeAcceptancePlan(),
      runtime_evidence_assessment: makeRuntimeEvidenceAssessment(),
    })

    expect(parsed.runtime_acceptance_plan?.checks[0].surface).toBe('api')
    expect(parsed.runtime_evidence_assessment?.external_ci_status).toBe('pending')
    expect(parsed.external_ci_status).toBe('passed')
  })

  it('rejects unknown runtime acceptance fields (strict)', () => {
    const bad = {
      ...sample,
      runtime_acceptance_plan: {
        ...makeRuntimeAcceptancePlan(),
        declares_ci_passed: true,
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('parses a semantic review verdict bound to the reviewed diff', () => {
    const parsed = changesetSchema.parse({
      ...sample,
      review_verdict: makeReviewVerdict(),
    })

    expect(parsed.review_verdict?.reviewed_diff_sha256).toBe('b'.repeat(64))
    expect(parsed.review_verdict?.overall_decision).toBe('rejected')
  })

  it('rejects semantic review payloads that disable deterministic overrides', () => {
    const bad = {
      ...sample,
      review_verdict: {
        ...makeReviewVerdict(),
        deterministic_errors_override_model: false,
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('rejects a semantic approval that conflicts with a deterministic error', () => {
    const bad = {
      ...sample,
      review_verdict: {
        ...makeReviewVerdict(),
        overall_decision: 'approved',
        actionable_instructions: [],
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('rejects unknown semantic review fields (strict)', () => {
    const bad = {
      ...sample,
      review_verdict: {
        ...makeReviewVerdict(),
        extra: true,
      },
    }

    expect(changesetSchema.safeParse(bad).success).toBe(false)
  })

  it('parses the fail-closed diff-truncation uncertainty', () => {
    const parsed = changesetSchema.parse({
      ...sample,
      review_verdict: makeReviewVerdict({
        uncertainties: [
          {
            uncertainty_id: 'RU-001',
            code: 'diff_truncated',
            requirement_ids: ['REQ-001'],
            evidence_ids: [],
            message: 'The diff exceeded the semantic review input budget.',
            resolution_instruction: 'Review the complete diff before publishing.',
          },
        ],
      }),
    })

    expect(parsed.review_verdict?.uncertainties[0].code).toBe('diff_truncated')
  })
})
