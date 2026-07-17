import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { toast } from 'sonner'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest'

import { runStatusSchema } from '../../src/api/schemas/agents'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { gateOutcome, MATRIX_ROWS } from '../../src/features/agents/gatingMatrix'
import { RunMonitorPage } from '../../src/features/agents/RunMonitorPage'
import { TriggerPage } from '../../src/features/agents/TriggerPage'
import { DecidePage } from '../../src/features/loop/DecidePage'
import { SteerPage } from '../../src/features/loop/SteerPage'
import { WatchPage } from '../../src/features/loop/WatchPage'
import type { Workspace } from '../../src/core/workspace'
import { makeReadOnlyAgentWorkspace, seedWorkspace } from '../helpers/fixtures'

const requests: { path: string; body: unknown }[] = []
let monitorStatus = 'waiting_approval'
let monitorPhase = 'experiment_design_approval'

function queuedApproval(runId: string) {
  return {
    command_id: '018f3d4e-c1c2-7000-8000-000000000001',
    run_id: runId,
    actor_credential_id: 'test-agents',
    actor_user_id: null,
    gate_id: `${runId}:experiment_design`,
    gate_agent: 'experiment_design',
    status: 'queued',
    approved_count: 1,
    rejected_count: 0,
    comment: null,
    last_error: null,
    created_at: '2026-07-15T00:00:00Z',
    updated_at: '2026-07-15T00:00:00Z',
    effects: [
      {
        effect_id: '018f3d4e-c1c2-7000-8000-000000000002',
        item_id: 'exp_cta',
        effect_type: 'stage_experiment_draft',
        status: 'queued',
        attempt_count: 0,
        last_error: null,
        result: null,
      },
    ],
  }
}

const server = setupServer(
  http.get('*/api/projects/demo/agents/v1/agents/definitions', () =>
    HttpResponse.json({ detail: 'Definitions unavailable' }, { status: 404 }),
  ),
  http.post('*/api/projects/demo/agents/v1/agents/trigger', async ({ request }) => {
    requests.push({ path: 'trigger', body: await request.json() })
    return HttpResponse.json({ run_id: 'run-abc-123', status: 'started' })
  }),
  http.get('*/api/projects/demo/agents/v1/agents/runs', ({ request }) => {
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
  http.get('*/api/projects/demo/agents/v1/agents/:runId/status', ({ params }) =>
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
  http.get('*/api/projects/demo/agents/v1/agents/:runId/results', () =>
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
  http.get('*/api/projects/demo/agents/v1/agents/:runId/audit', () =>
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
  http.post('*/api/projects/demo/agents/v1/agents/:runId/approve', async ({ request, params }) => {
    requests.push({ path: 'approve', body: await request.json() })
    monitorStatus = 'approval_queued'
    return HttpResponse.json(queuedApproval(String(params.runId)))
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  vi.restoreAllMocks()
})
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  requests.length = 0
  monitorStatus = 'waiting_approval'
  monitorPhase = 'experiment_design_approval'
})

function renderWithProviders(
  ui: React.ReactElement,
  initialPath: string,
  workspace: Workspace = seedWorkspace(),
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider initialWorkspaces={[workspace]}>
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
    // L3 deploys low risk only; L4 deploys any safety-passing risk level.
    expect(gateOutcome(3, 'low')).toBe('deploy')
    expect(gateOutcome(4, 'low')).toBe('deploy')
    expect(gateOutcome(3, 'medium')).toBe('approve')
    expect(gateOutcome(4, 'high')).toBe('deploy')
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
  test('fails closed on a direct trigger URL without agents:run', () => {
    renderWithProviders(
      <TriggerPage />,
      '/agents/trigger',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(screen.getByText('Agent execution unavailable')).toBeInTheDocument()
    expect(screen.getByText(/does not grant agents:run/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start run' })).not.toBeInTheDocument()
    expect(requests.some((entry) => entry.path === 'trigger')).toBe(false)
  })

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
    // Default mode runs the full built-in loop when definition discovery is
    // unavailable, so the built-in fallback list is selected.
    expect(requests[0]?.body).toEqual({
      project_id: 'demo',
      trigger_type: 'manual',
      analysis_types: [
        'behavior_analysis',
        'experiment_design',
        'feature_proposal',
      ],
      time_range_days: 7,
      autonomy_level: 2,
    })
  })

  test('filters experiment evaluation from live definitions and trigger payloads', async () => {
    server.use(
      http.get('*/api/projects/demo/agents/v1/agents/definitions', () =>
        HttpResponse.json({
          agents: [
            {
              name: 'behavior_analysis',
              display_name: 'Behavior analysis',
              description: 'Produces insights.',
              order: 10,
              produces: 'insights',
              requires: [],
              model_tier: 'reasoning',
              is_custom: false,
            },
            {
              name: 'experiment_evaluation',
              display_name: 'Experiment evaluation',
              description: 'Produces experiment verdicts.',
              order: 30,
              produces: 'experiment_verdicts',
              requires: ['experiment_designs'],
              model_tier: 'reasoning',
              is_custom: false,
            },
          ],
          tool_catalog: [],
        }),
      ),
    )
    renderWithProviders(
      <Routes>
        <Route path="/agents/trigger" element={<TriggerPage />} />
        <Route path="/agents/runs/:runId" element={<div>monitor page</div>} />
      </Routes>,
      '/agents/trigger',
    )

    expect(await screen.findByText('Produces insights.')).toBeInTheDocument()
    expect(screen.queryByText('Experiment evaluation')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }))
    expect(await screen.findByText('monitor page')).toBeInTheDocument()
    expect(requests.find((entry) => entry.path === 'trigger')?.body).toMatchObject({
      analysis_types: ['behavior_analysis'],
    })
  })
})

describe('RunMonitorPage', () => {
  test('keeps a pending approval read-only without agents:approve', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(await screen.findByText(/experiment design awaiting operator approval/i)).toBeInTheDocument()
    expect((await screen.findAllByText('Bigger CTA converts better')).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/requires agents:approve/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /submit decisions/i })).not.toBeInTheDocument()
    expect(requests.some((entry) => entry.path === 'approve')).toBe(false)
  })

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
    expect(await screen.findByText('approval queued')).toBeInTheDocument()
  })

  test('reports a committed command without claiming deployment completed', async () => {
    const success = vi.spyOn(toast, 'success').mockReturnValue('approval-queued')
    server.use(
      http.post('*/api/projects/demo/agents/v1/agents/:runId/approve', ({ params }) =>
        HttpResponse.json(queuedApproval(String(params.runId))),
      ),
    )
    renderWithProviders(
      <Routes>
        <Route path="/agents/runs/:runId" element={<RunMonitorPage />} />
      </Routes>,
      '/agents/runs/run-abc-123',
    )

    expect(await screen.findByText(/experiment design awaiting approval/i)).toBeInTheDocument()
    expect(await screen.findByText("What you're approving (1)")).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /submit decisions/i }))

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith(
        expect.stringContaining('effects queued'),
        expect.objectContaining({ description: expect.stringContaining('command') }),
      ),
    )
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
  test('preserves history but removes start controls without agents:run', async () => {
    const { RunsPage } = await import('../../src/features/agents/RunsPage')
    renderWithProviders(
      <Routes>
        <Route path="/agents" element={<RunsPage />} />
      </Routes>,
      '/agents',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(await screen.findByText(/run-srv-/)).toBeInTheDocument()
    expect(screen.getByText(/run history is read-only/i)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /trigger run/i })).not.toBeInTheDocument()
  })

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

describe('read-only loop surfaces', () => {
  test('Watch preserves run activity but removes run-loop CTAs', async () => {
    renderWithProviders(
      <WatchPage />,
      '/watch',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(await screen.findByText(/run-srv-/)).toBeInTheDocument()
    expect(screen.getByText(/loop activity is read-only/i)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /run loop/i })).not.toBeInTheDocument()
  })

  test('Steer removes execution configuration but keeps definitions read-only', () => {
    renderWithProviders(
      <SteerPage />,
      '/steer',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(screen.getByText(/starting or configuring agent execution requires agents:run/i)).toBeInTheDocument()
    expect(screen.queryByText('Configure a run →')).not.toBeInTheDocument()
    expect(screen.getByText('View agents →')).toBeInTheDocument()
  })

  test('Decide shows pending decisions without approval actions', async () => {
    server.use(
      http.get('*/api/projects/demo/agents/v1/agents/runs', () =>
        HttpResponse.json({
          runs: [
            {
              run_id: 'run-abc-123',
              project_id: 'demo',
              status: 'waiting_approval',
              phase: 'experiment_design_approval',
              insights_count: 4,
              experiments_count: 1,
              started_at: '2026-06-10T12:00:00+00:00',
              updated_at: '2026-06-10T12:01:00+00:00',
            },
          ],
          count: 1,
        }),
      ),
    )
    renderWithProviders(
      <DecidePage />,
      '/decide',
      seedWorkspace(makeReadOnlyAgentWorkspace()),
    )

    expect(await screen.findByText('Waiting on an operator')).toBeInTheDocument()
    expect(await screen.findByText(/Run the "exp_cta" experiment/i)).toBeInTheDocument()
    expect(screen.getByText(/decisions are read-only/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /approve design/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reject' })).not.toBeInTheDocument()
  })

  test('Decide reports a durable command without claiming synchronous effects', async () => {
    const success = vi.spyOn(toast, 'success').mockReturnValue('decision-queued')
    server.use(
      http.get('*/api/projects/demo/agents/v1/agents/runs', () =>
        HttpResponse.json({
          runs: [
            {
              run_id: 'run-abc-123',
              project_id: 'demo',
              status: 'waiting_approval',
              phase: 'experiment_design_approval',
              insights_count: 4,
              experiments_count: 1,
              started_at: '2026-06-10T12:00:00+00:00',
              updated_at: '2026-06-10T12:01:00+00:00',
            },
          ],
          count: 1,
        }),
      ),
      http.post('*/api/projects/demo/agents/v1/agents/:runId/approve', ({ params }) =>
        HttpResponse.json(queuedApproval(String(params.runId))),
      ),
    )
    renderWithProviders(<DecidePage />, '/decide')

    await userEvent.click(await screen.findByRole('button', { name: /approve design/i }))

    await waitFor(() =>
      expect(success).toHaveBeenCalledWith(
        expect.stringContaining('effects queued'),
        expect.objectContaining({ description: expect.stringContaining('command') }),
      ),
    )
  })
})
