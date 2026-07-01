import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ChangesetDetailPage } from '../../src/features/codegen/ChangesetDetailPage'
import { seedWorkspace } from '../helpers/fixtures'

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
    status: 'tests_failed',
    base_branch: 'main',
    branch: null,
    pr_url: null,
    pr_number: null,
    pr_node_id: null,
    ci_status: null,
    diff_stat: {},
    error: 'verification failed (`npm run build`):\nDid you mean to import hashBucket?',
    created_at: '2026-07-01T03:15:31.000000Z',
    updated_at: '2026-07-01T03:21:35.000000Z',
    ...overrides,
  }
}

const server = setupServer(
  http.get('http://localhost:8084/v1/changesets/:id', () => HttpResponse.json(makeChangeset())),
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
    <WorkspaceProvider>
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
  test('surfaces the full failure reason for a tests_failed run', async () => {
    renderDetail()
    expect(await screen.findByText('Automated Non-Organic Traffic Detection')).toBeInTheDocument()
    expect(screen.getByText('Failure reason')).toBeInTheDocument()
    // The whole error string is shown verbatim, not truncated away.
    expect(
      screen.getByText(/verification failed \(`npm run build`\):/),
    ).toBeInTheDocument()
    expect(screen.getByText(/Did you mean to import hashBucket\?/)).toBeInTheDocument()
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

  test('shows a not-found state for an unknown changeset id', async () => {
    server.use(
      http.get('http://localhost:8084/v1/changesets/:id', () => new HttpResponse(null, { status: 404 })),
    )
    renderDetail('/codegen/cs_missing')
    expect(await screen.findByText('Changeset not found')).toBeInTheDocument()
  })
})
