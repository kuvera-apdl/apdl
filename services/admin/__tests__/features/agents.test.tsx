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
import { loadTrackedRuns } from '../../src/features/agents/runHistory'
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
  http.post('http://localhost:8083/v1/agents/:runId/approve', async ({ request, params }) => {
    requests.push({ path: 'approve', body: await request.json() })
    monitorStatus = 'approved'
    monitorPhase = 'resuming'
    return HttpResponse.json({
      run_id: String(params.runId),
      status: 'approved',
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
  test('posts the manual trigger and tracks the run locally', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/trigger" element={<TriggerPage />} />
        <Route path="/agents/runs/:runId" element={<div>monitor page</div>} />
      </Routes>,
      '/agents/trigger',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(await screen.findByText('monitor page')).toBeInTheDocument()
    expect(requests[0]?.body).toEqual({
      project_id: 'demo',
      trigger_type: 'manual',
      analysis_types: ['behavior_analysis'],
      time_range_days: 7,
      autonomy_level: 2,
    })
    expect(loadTrackedRuns('ws-test')[0]).toMatchObject({
      run_id: 'run-abc-123',
      autonomy_level: 2,
    })
  })
})

describe('RunMonitorPage', () => {
  test('shows the approval gate and approves', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
    )

    expect(await screen.findByText(/experiment design awaiting approval/i)).toBeInTheDocument()
    // Reject requires a comment.
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() =>
      expect(requests.some((entry) => entry.path === 'approve')).toBe(true),
    )
    expect(requests.find((entry) => entry.path === 'approve')?.body).toEqual({ approved: true })

    // After approval the panel goes away on the refetch.
    expect(await screen.findByText('approved')).toBeInTheDocument()
  })

  test('renders counters with the G3 honesty note', async () => {
    monitorStatus = 'running'
    monitorPhase = 'behavior_analysis'
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
    )
    expect(await screen.findByText('4')).toBeInTheDocument()
    expect(screen.getByText(/run-results endpoint/)).toBeInTheDocument()
  })
})
