import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ChangesetsPage } from '../../src/features/codegen/ChangesetsPage'
import { seedWorkspace } from '../helpers/fixtures'

function makeChangeset(overrides: Record<string, unknown> = {}) {
  return {
    changeset_id: 'cs_list_1',
    project_id: 'demo',
    run_id: null,
    task: { title: 'Add strict schema', spec: 'Reject unknown fields.', context: {}, constraints: [] },
    status: 'pr_open',
    base_branch: 'main',
    branch: 'apdl/strict-schema',
    pr_url: 'https://github.com/acme/widgets/pull/17',
    pr_number: 17,
    head_sha: 'c'.repeat(40),
    github_pr_status: 'open',
    external_ci_status: 'unverified_external_ci',
    external_ci_awaiting_since: '2026-07-11T14:00:00+00:00',
    ci_retry_count: 0,
    ci_remediation_status: 'idle',
    ci_failure_key: null,
    ci_failure_summary: null,
    merge_sha: null,
    diff_stat: { files: 2 },
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
    publication_authorization: null,
    error: null,
    created_at: '2026-07-11T13:00:00+00:00',
    updated_at: '2026-07-11T14:00:00+00:00',
    ...overrides,
  }
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  server.use(
    http.get(
      '*/api/projects/demo/codegen/v1/connections/demo',
      () => new HttpResponse(null, { status: 404 }),
    ),
  )
})

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter>
            <ChangesetsPage />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('ChangesetsPage', () => {
  test('separates lifecycle, GitHub PR, and external CI without app PR controls', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets', () =>
        HttpResponse.json([makeChangeset()]),
      ),
    )

    renderPage()
    expect(await screen.findByText('Add strict schema')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Lifecycle' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'GitHub PR' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'External CI' })).toBeInTheDocument()
    expect(screen.getByText('no CI configured')).toBeInTheDocument()
    expect(screen.getByText('cccccccccccc')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open PR on GitHub' })).toHaveAttribute(
      'href',
      'https://github.com/acme/widgets/pull/17',
    )
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Abandon' })).not.toBeInTheDocument()
  })

  test('keeps retry and abandon limited to pre-PR error and queued work', async () => {
    server.use(
      http.get('*/api/projects/demo/codegen/v1/changesets', () =>
        HttpResponse.json([
          makeChangeset({
            changeset_id: 'cs_error',
            task: { title: 'Failed generation', spec: 'Retry me.', context: {}, constraints: [] },
            status: 'error',
            branch: null,
            pr_url: null,
            pr_number: null,
            head_sha: null,
            github_pr_status: null,
            external_ci_status: null,
            external_ci_awaiting_since: null,
            error: 'generation failed',
          }),
          makeChangeset({
            changeset_id: 'cs_queued',
            task: { title: 'Queued generation', spec: 'Waiting.', context: {}, constraints: [] },
            status: 'queued',
            branch: null,
            pr_url: null,
            pr_number: null,
            head_sha: null,
            github_pr_status: null,
            external_ci_status: null,
            external_ci_awaiting_since: null,
          }),
        ]),
      ),
    )

    renderPage()
    expect(await screen.findByText('Failed generation')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Abandon' })).toBeInTheDocument()
  })
})
