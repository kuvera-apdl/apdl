import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { runStatusSchema } from '../../src/api/schemas/agents'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { gateOutcome, MATRIX_ROWS } from '../../src/features/agents/gatingMatrix'
import { RunMonitorPage } from '../../src/features/agents/RunMonitorPage'
import { TriggerPage } from '../../src/features/agents/TriggerPage'
import { seedWorkspace } from '../helpers/fixtures'

const requests: { path: string; body: unknown }[] = []
let monitorStatus = 'waiting_approval'
let monitorPhase = 'experiment_design_approval'

const server = setupServer(
  http.post('http://localhost:8083/v1/agents/trigger', async ({ request }) => {
    requests.push({ path: 'trigger', body: await request.json() })
    return HttpResponse.json({ run_id: 'run-abc-123', status: 'started' })
  }),
  http.get('http://localhost:8083/v1/agents/runs', ({ request }) => {
    const url = new URL(request.url)
    requests.push({ path: 'runs-list', body: Object.fromEntries(url.searchParams) })
    const runs = [
      {
        run_id: 'run-srv-1',
        project_id: 'demo',
        status: 'completed',
        phase: 'done',
        insights_count: 3,
        experiments_count: 1,
        started_at: '2026-06-10T11:00:00+00:00',
        updated_at: '2026-06-10T11:05:00+00:00',
      },
    ].filter((run) => !url.searchParams.get('status') || run.status === url.searchParams.get('status'))
    return HttpResponse.json({ runs, count: runs.length })
  }),
  http.get('http://localhost:8083/v1/agents/:runId/status', ({ params }) =>
    HttpResponse.json({
      run_id: String(params.runId),
      project_id: 'demo',
      status: monitorStatus,
      phase: monitorPhase,
      insights_count: 4,
      experiments_count: 1,
      started_at: '2026-06-10T12:00:00+00:00',
      updated_at: '2026-06-10T12:01:00+00:00',
    }),
  ),
  http.get('http://localhost:8083/v1/agents/:runId/results', () =>
    HttpResponse.json({
      run_id: 'run-abc-123',
      insights: [{ title: 'Checkout drop-off', severity: 'high' }],
      experiment_designs: [
        { experiment_id: 'exp_cta', hypothesis: 'Bigger CTA converts better', flag_key: 'checkout-cta', metric: 'purchase' },
      ],
      personalizations: [],
      feature_proposals: [],
      changesets: [],
    }),
  ),
  http.get('http://localhost:8083/v1/agents/:runId/audit', () =>
    HttpResponse.json({
      run_id: 'run-abc-123',
      audit: [
        {
          id: 1,
          run_id: 'run-abc-123',
          action_type: 'experiment_design_complete',
          config: { produced: 'experiment_designs' },
          safety_result: {
            risk_level: 'medium',
            checks: [{ name: 'blast_radius', passed: true }],
          },
          approval_status: 'pending',
          created_at: '2026-06-10T12:01:00+00:00',
        },
      ],
      count: 1,
    }),
  ),
  http.post('http://localhost:8083/v1/agents/:runId/approve', async ({ request, params }) => {
    requests.push({ path: 'approve', body: await request.json() })
    monitorStatus = 'approved'
    monitorPhase = 'resuming'
    return HttpResponse.json({
      run_id: String(params.runId),
      status: 'approved',
      approved_count: 1,
      rejected_count: 0,
      forked_runs: [],
      opened_changesets: [],
      message: 'Run approved',
    })
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  requests.length = 0
  monitorStatus = 'waiting_approval'
  monitorPhase = 'experiment_design_approval'
})

function renderWithProviders(ui: React.ReactElement, initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[initialPath]}>{ui}</MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('gating matrix (must match framework/gating.py)', () => {
  test('encodes the gate exactly', () => {
    // Failed safety is halt at every level (first matrix row).
    expect(MATRIX_ROWS[0]!.outcomes(4)).toBe('halt')
    // L1 is suggest-only.
    expect(gateOutcome(1, 'low')).toBe('halt')
    // L2 holds everything for approval — even low risk.
    expect(gateOutcome(2, 'low')).toBe('approve')
    // Only L>=3 + low risk deploys.
    expect(gateOutcome(3, 'low')).toBe('deploy')
    expect(gateOutcome(4, 'low')).toBe('deploy')
    // Medium/high never auto-deploys — L4 behaves like L3 today.
    expect(gateOutcome(3, 'medium')).toBe('approve')
    expect(gateOutcome(4, 'high')).toBe('approve')
    // Feature proposals always gate, regardless of level.
    expect(gateOutcome(4, 'low', true)).toBe('approve')
  })
})

describe('runStatusSchema', () => {
  test('parses the status payload', () => {
    expect(
      runStatusSchema.safeParse({
        run_id: 'r',
        project_id: 'demo',
        status: 'running',
        phase: 'behavior_analysis',
        insights_count: 0,
        experiments_count: 0,
        started_at: '2026-06-10T12:00:00+00:00',
        updated_at: '2026-06-10T12:00:30+00:00',
      }).success,
    ).toBe(true)
  })
})

describe('TriggerPage', () => {
  test('posts the manual trigger and navigates to the run', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/trigger" element={<TriggerPage />} />
        <Route path="/agents/runs/:runId" element={<div>monitor page</div>} />
      </Routes>,
      '/agents/trigger',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }))

    // Navigates to the new run — no client-side run tracking (the server
    // owns run history now).
    expect(await screen.findByText('monitor page')).toBeInTheDocument()
    // Default mode runs the full built-in loop (the definitions endpoint is
    // unmocked here, so the built-in fallback list is what's selected).
    expect(requests[0]?.body).toEqual({
      project_id: 'demo',
      trigger_type: 'manual',
      analysis_types: [
        'behavior_analysis',
        'experiment_design',
        'experiment_evaluation',
        'feature_proposal',
      ],
      time_range_days: 7,
      autonomy_level: 2,
    })
  })
})

describe('RunMonitorPage', () => {
  test('renders the per-item approval panel and submits batched decisions', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
    )

    expect(await screen.findByText(/experiment design awaiting approval/i)).toBeInTheDocument()
    // The panel shows WHAT is being approved (gap G3), per item. The design
    // also appears in the Outputs card, hence getAllByText.
    expect(await screen.findByText("What you're approving (1)")).toBeInTheDocument()
    expect(screen.getAllByText('Bigger CTA converts better').length).toBeGreaterThanOrEqual(1)

    // Default verdict is approve; one batched submit sends a decision per item.
    await userEvent.click(screen.getByRole('button', { name: /submit decisions/i }))
    await waitFor(() =>
      expect(requests.some((entry) => entry.path === 'approve')).toBe(true),
    )
    expect(requests.find((entry) => entry.path === 'approve')?.body).toEqual({
      decisions: [{ item_id: 'exp_cta', approved: true }],
    })

    // After approval the panel goes away on the refetch.
    expect(await screen.findByText('approved')).toBeInTheDocument()
  })

  test('renders persisted outputs and the agent audit trail', async () => {
    monitorStatus = 'completed'
    monitorPhase = 'done'
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
    )
    expect(await screen.findByText('Outputs')).toBeInTheDocument()
    expect(screen.getByText('Checkout drop-off')).toBeInTheDocument()
    // Audit trail (gap G2) with safety checks rendered pass/fail.
    expect(await screen.findByText('experiment_design_complete')).toBeInTheDocument()
    expect(screen.getByText('blast_radius')).toBeInTheDocument()
    expect(screen.getByText('pending')).toBeInTheDocument()
  })
})

describe('RunsPage (server list, gap G1)', () => {
  test('lists runs from the server with the project filter applied', async () => {
    const { RunsPage } = await import('../../src/features/agents/RunsPage')
    renderWithProviders(
      <Routes>
        <Route path="/agents" element={<RunsPage />} />
      </Routes>,
      '/agents',
    )
    expect(await screen.findByText(/run-srv-/)).toBeInTheDocument()
    // "completed" appears in both the status filter <option> and the pill.
    expect(screen.getAllByText('completed').length).toBeGreaterThanOrEqual(2)
    const listCall = requests.find((entry) => entry.path === 'runs-list')
    expect(listCall?.body).toMatchObject({ project_id: 'demo', limit: '50' })
  })
})
