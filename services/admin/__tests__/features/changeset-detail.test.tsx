import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ChangesetDetailPage } from '../../src/features/codegen/ChangesetDetailPage'
import {
  makeChangesetObservationHistory,
  makeReviewVerdict,
  makeRuntimeAcceptancePlan,
  makeRuntimeEvidenceAssessment,
  makeRuntimeEvidenceObservation,
  makeVerificationCoverage,
  makeVerificationPlan,
  seedWorkspace,
} from '../helpers/fixtures'

function makeChangeset(overrides: Record<string, unknown> = {}) {
  return {
    changeset_id: 'cs_abc123',
    project_id: 'demo',
    run_id: 'run-9999',
    task: {
      title: 'Automated Non-Organic Traffic Detection',
      spec:
        'Build a traffic-quality exclusion layer.\n\n' +
        '{"dependencies": ["Resolution of the $click anomaly"], "estimated_effort": "small", ' +
        '"components_affected": ["Analytics query layer"], "technical_considerations": ["Percentile reporting"]}',
      context: {},
      constraints: ['All existing tests must pass.'],
    },
    status: 'error',
    base_branch: 'main',
    branch: null,
    pr_url: null,
    pr_number: null,
    head_sha: null,
    github_pr_status: null,
    external_ci_status: null,
    external_ci_awaiting_since: null,
    ci_retry_count: 0,
    ci_remediation_status: 'idle',
    ci_failure_key: null,
    ci_failure_summary: null,
    merge_sha: null,
    diff_stat: {},
    prompts: [],
    contract_bundle: null,
    requirement_ledger: null,
    inspection_snapshot: null,
    dependency_slice: null,
    verification_plan: null,
    verification_coverage: null,
    runtime_acceptance_plan: null,
    runtime_evidence_assessment: null,
    review_verdict: null,
    error: 'verification failed (`npm run build`):\nDid you mean to import hashBucket?',
    created_at: '2026-07-01T03:15:31.000000Z',
    updated_at: '2026-07-01T03:21:35.000000Z',
    ...overrides,
  }
}

const server = setupServer(
  http.get('*/api/projects/demo/codegen/v1/changesets/:id', () => HttpResponse.json(makeChangeset())),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
})

function renderDetail(path = '/codegen/cs_abc123') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/codegen/:id" element={<ChangesetDetailPage />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('ChangesetDetailPage', () => {
  test('surfaces the full failure reason and retry for a pre-PR error', async () => {
    renderDetail()
    expect(await screen.findByText('Automated Non-Organic Traffic Detection')).toBeInTheDocument()
    expect(screen.getByText('Failure reason')).toBeInTheDocument()
    // The whole error string is shown verbatim, not truncated away.
    expect(
      screen.getByText(/verification failed \(`npm run build`\):/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Did you mean to import hashBucket\?/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Abandon' })).not.toBeInTheDocument()
  })

  test('splits the trailing JSON metadata out of the spec prose', async () => {
    renderDetail()
    // Prose renders without the JSON blob leaking into it.
    const prose = await screen.findByText(/Build a traffic-quality exclusion layer\./)
    expect(prose.textContent).not.toContain('estimated_effort')
    // Metadata renders in its structured shape.
    expect(screen.getByText('small')).toBeInTheDocument()
    expect(screen.getByText('Resolution of the $click anomaly')).toBeInTheDocument()
    expect(screen.getByText('All existing tests must pass.')).toBeInTheDocument()
  })

  test('renders the recorded prompt transcript with system and user prompts', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            prompts: [
              {
                stage: 'brief',
                label: 'Brief compilation (spec → engineering brief)',
                system: 'You compile approved product feature proposals into precise engineering briefs',
                user: '# Approved proposal\n\n## Title\nAutomated Non-Organic Traffic Detection',
                notes: null,
              },
              {
                stage: 'edit',
                label: 'Edit instruction (attempt 1)',
                system: null,
                user: '## Goal\nShip the traffic-quality exclusion layer.',
                notes:
                  "The system prompt for this step is Aider's built-in editing prompt (not authored by APDL). " +
                  'Read-only context files attached: CONVENTIONS.md.',
              },
            ],
          }),
        ),
      ),
    )
    renderDetail()
    expect(await screen.findByText('Brief compilation (spec → engineering brief)')).toBeInTheDocument()
    expect(screen.getByText('Edit instruction (attempt 1)')).toBeInTheDocument()
    // Both halves of the pair are shown: the system prompt and the user message.
    expect(
      screen.getByText(/You compile approved product feature proposals/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Ship the traffic-quality exclusion layer\./)).toBeInTheDocument()
    // The edit stage is honest about whose system prompt runs there.
    expect(screen.getByText(/Aider's built-in editing prompt/)).toBeInTheDocument()
  })

  test('explains an empty prompt transcript instead of hiding the section', async () => {
    renderDetail()
    await screen.findByText('Automated Non-Organic Traffic Detection')
    expect(screen.getByText('Prompts')).toBeInTheDocument()
    expect(screen.getByText(/No prompts recorded for this run yet/)).toBeInTheDocument()
  })

  test('shows the verification plan and pre-CI coverage without claiming CI passed', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            status: 'pr_open',
            verification_plan: makeVerificationPlan(),
            verification_coverage: makeVerificationCoverage(),
          }),
        ),
      ),
    )

    renderDetail()
    expect(await screen.findByText('Verification plan')).toBeInTheDocument()
    expect(screen.getByText('GitHub CI planned')).toBeInTheDocument()
    expect(screen.getByText('Reject payloads with unknown fields.')).toBeInTheDocument()
    expect(screen.getByText('Pre-CI coverage')).toBeInTheDocument()
    expect(screen.getByText('Ready for GitHub CI')).toBeInTheDocument()
    expect(screen.getAllByText('src/api/__tests__/schema.test.ts')).toHaveLength(2)
    expect(screen.getByText(/GitHub remains authoritative/)).toBeInTheDocument()
    expect(screen.queryByText('Passed')).not.toBeInTheDocument()
  })

  test('shows the semantic verdict as a pre-push review rather than GitHub CI', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            status: 'editing',
            error: null,
            review_verdict: makeReviewVerdict(),
          }),
        ),
      ),
    )

    renderDetail()
    expect(await screen.findByText('Semantic review')).toBeInTheDocument()
    expect(screen.getAllByText('Rejected')).toHaveLength(2)
    expect(screen.getByText('The generated diff initializes a resource but does not release it.')).toBeInTheDocument()
    expect(screen.getByText('b'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText(/This is not a GitHub CI result/)).toBeInTheDocument()
    expect(screen.queryByText('CI passed')).not.toBeInTheDocument()
  })

  test('shows runtime plans and exact-head evidence without promoting external CI', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            status: 'pr_open',
            error: null,
            pr_url: 'https://github.com/acme/widgets/pull/17',
            pr_number: 17,
            head_sha: 'c'.repeat(40),
            github_pr_status: 'open',
            external_ci_status: 'pending',
            runtime_acceptance_plan: makeRuntimeAcceptancePlan(),
            runtime_evidence_assessment: makeRuntimeEvidenceAssessment(),
          }),
        ),
      ),
      http.get('*/api/projects/demo/codegen/v1/changesets/:id/observations', () =>
        HttpResponse.json(makeChangesetObservationHistory()),
      ),
      http.get('*/api/projects/demo/codegen/v1/changesets/:id/runtime-observations', () =>
        HttpResponse.json([makeRuntimeEvidenceObservation()]),
      ),
    )

    renderDetail()
    expect(await screen.findByText('Runtime acceptance plan')).toBeInTheDocument()
    expect(screen.getByText('npm run test:runtime')).toBeInTheDocument()
    expect(await screen.findByText('Runtime acceptance evidence')).toBeInTheDocument()
    expect(screen.getByText('Current GitHub-owned external CI:')).toBeInTheDocument()
    expect(screen.getAllByText('pending').length).toBeGreaterThan(0)
    expect(screen.getAllByText('apdl-runtime-REQ-001').length).toBeGreaterThan(0)
    expect(screen.getByText('Bounded job-log excerpts')).toBeInTheDocument()
    expect(screen.getByText(/Runtime evidence never promotes or replaces/)).toBeInTheDocument()
    expect(screen.queryByText('Runtime passed')).not.toBeInTheDocument()
  })

  test('renders append-only PR, exact-head CI, and remediation observations', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            status: 'pr_open',
            error: null,
            pr_url: 'https://github.com/acme/widgets/pull/17',
            pr_number: 17,
            head_sha: 'c'.repeat(40),
            github_pr_status: 'open',
            external_ci_status: 'failed',
            external_ci_awaiting_since: '2026-07-11T14:00:01+00:00',
            ci_remediation_status: 'awaiting_ci',
          }),
        ),
      ),
      http.get('*/api/projects/demo/codegen/v1/changesets/:id/observations', () =>
        HttpResponse.json(makeChangesetObservationHistory()),
      ),
    )

    renderDetail()
    expect(await screen.findByText('GitHub observation history')).toBeInTheDocument()
    expect(screen.getByText('Pull request events')).toBeInTheDocument()
    expect(screen.getByText('External CI observations')).toBeInTheDocument()
    expect(screen.getByText('Remediation events')).toBeInTheDocument()
    expect(document.body).toHaveTextContent('Expected unknown fields to be rejected.')
    expect(screen.getByText(/GitHub test failed on the exact pull-request head/)).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /cccccccccccc/i }).length).toBeGreaterThan(0)
    expect(screen.getByRole('link', { name: 'Open PR on GitHub' })).toHaveAttribute(
      'href',
      'https://github.com/acme/widgets/pull/17',
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Abandon' })).not.toBeInTheDocument()
  })

  test('warns that a head with no configured CI is unverified, never passed', async () => {
    const history = makeChangesetObservationHistory({
      ci_verifications: [
        {
          schema_version: 'ci_verification_observation@1',
          observation_id: 'ci_observation:no-ci',
          changeset_id: 'cs_abc123',
          repository: 'acme/widgets',
          pr_number: 17,
          head_sha: 'c'.repeat(40),
          status: 'unverified_external_ci',
          signals: [],
          requirement_results: [],
          observed_at: '2026-07-11T14:01:00+00:00',
          failure_key: null,
          failure_summary: null,
        },
      ],
      remediation_attempts: [],
    })
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(
          makeChangeset({
            status: 'pr_open',
            error: null,
            pr_url: 'https://github.com/acme/widgets/pull/17',
            pr_number: 17,
            head_sha: 'c'.repeat(40),
            github_pr_status: 'open',
            external_ci_status: 'unverified_external_ci',
          }),
        ),
      ),
      http.get('*/api/projects/demo/codegen/v1/changesets/:id/observations', () =>
        HttpResponse.json(history),
      ),
    )

    renderDetail()
    expect(await screen.findByText('No external CI configured')).toBeInTheDocument()
    expect(screen.getByText(/absence of CI is never represented as passed/)).toBeInTheDocument()
    expect(await screen.findByText('GitHub observation history')).toBeInTheDocument()
    expect(document.body).toHaveTextContent('No CI signals were configured or observed for this head.')
  })

  test('allows abandon only while pre-PR work is queued', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () =>
        HttpResponse.json(makeChangeset({ status: 'queued', error: null })),
      ),
    )

    renderDetail()
    expect(await screen.findByRole('button', { name: 'Abandon' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
  })

  test('shows a not-found state for an unknown changeset id', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets/:id', () => new HttpResponse(null, { status: 404 })),
    )
    renderDetail('/codegen/cs_missing')
    expect(await screen.findByText('Changeset not found')).toBeInTheDocument()
  })
})
