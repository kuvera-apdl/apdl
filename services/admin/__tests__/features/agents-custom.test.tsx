// Custom agents: list/manage page, the creation wizard (incl. dry-run), and
// the trigger page's merged built-in + custom listing.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import type { CustomAgent } from '../../src/api/types/agents'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { CustomAgentsPage } from '../../src/features/agents/custom/CustomAgentsPage'
import { CustomAgentWizardPage } from '../../src/features/agents/custom/CustomAgentWizardPage'
import { TriggerPage } from '../../src/features/agents/TriggerPage'
import { seedWorkspace } from '../helpers/fixtures'

const BASE = 'http://localhost:8083'
const QUERY_BASE = 'http://localhost:8082'

function makeCustomAgent(overrides: Partial<CustomAgent> = {}): CustomAgent {
  return {
    agent_id: 'agent-1',
    project_id: 'demo',
    slug: 'churn_watch',
    display_name: 'Churn watch',
    description: 'Watches churn signals',
    system_prompt: 'You are a churn analyst.',
    user_prompt_template: 'Analyse churn for {project_id}',
    model_tier: 'fast',
    tools: ['discover_events'],
    preset_tools: [],
    requires: [],
    produces: 'churn_signals',
    memory_query: null,
    memory_top_k: 5,
    pipeline_order: 60,
    max_tool_steps: 8,
    status: 'active',
    created_at: '2026-07-01T10:00:00+00:00',
    updated_at: '2026-07-01T10:00:00+00:00',
    ...overrides,
  }
}

const DEFINITIONS = {
  agents: [
    {
      name: 'behavior_analysis',
      display_name: 'Behavior analysis',
      description: 'Reads event history and produces insights.',
      order: 10,
      produces: 'insights',
      requires: [],
      model_tier: 'reasoning',
      is_custom: false,
      agent_id: null,
    },
    {
      name: 'churn_watch',
      display_name: 'Churn watch',
      description: 'Watches churn signals',
      order: 60,
      produces: 'churn_signals',
      requires: [],
      model_tier: 'fast',
      is_custom: true,
      agent_id: 'agent-1',
    },
  ],
  tool_catalog: [
    {
      name: 'discover_events',
      description: 'List the event names present for the project.',
      params_schema: { properties: { limit: { type: 'integer' } }, required: [] },
    },
    {
      name: 'query_funnel',
      description: 'Multi-step funnel conversion analysis.',
      params_schema: {
        properties: { steps: { type: 'array' }, window_days: { type: 'integer' } },
        required: ['steps'],
      },
    },
  ],
}

const requests: { path: string; method: string; body: unknown }[] = []
let customAgents: CustomAgent[] = []

const server = setupServer(
  // Event catalog behind the preset-query event pickers (EventCombobox).
  http.post(`${QUERY_BASE}/v1/query/events/names`, () =>
    HttpResponse.json({
      events: [
        { event_name: 'signup', event_count: 100, unique_users: 80 },
        { event_name: 'purchase', event_count: 40, unique_users: 30 },
      ],
    }),
  ),
  http.get(`${BASE}/v1/agents/definitions`, () => HttpResponse.json(DEFINITIONS)),
  http.get(`${BASE}/v1/agents/custom`, () => HttpResponse.json(customAgents)),
  http.post(`${BASE}/v1/agents/custom`, async ({ request }) => {
    const body = await request.json()
    requests.push({ path: 'create', method: 'POST', body })
    return HttpResponse.json(
      makeCustomAgent(body as Partial<CustomAgent>),
      { status: 201 },
    )
  }),
  http.post(`${BASE}/v1/agents/custom/test`, async ({ request }) => {
    requests.push({ path: 'test', method: 'POST', body: await request.json() })
    return HttpResponse.json({
      prompt: 'Analyse [...]',
      raw_response: '[{"signal": "activation drop"}]',
      parsed_output: [{ signal: 'activation drop' }],
      preset_results: [
        {
          tool: 'query_funnel',
          params: { steps: [{ event_name: 'signup' }, { event_name: 'purchase' }] },
          result: '{"steps": 2}',
          error: null,
          elapsed_ms: 20,
        },
      ],
      tool_results: [
        {
          tool: 'discover_events',
          params: { limit: 5 },
          result: '{"events": ["signup"]}',
          error: null,
          elapsed_ms: 12,
        },
      ],
      timings_ms: { llm: 900, total: 950 },
    })
  }),
  http.get(`${BASE}/v1/agents/custom/:agentId`, ({ params }) => {
    const agent = customAgents.find((entry) => entry.agent_id === params.agentId)
    return agent
      ? HttpResponse.json(agent)
      : HttpResponse.json({ detail: 'Custom agent not found' }, { status: 404 })
  }),
  http.delete(`${BASE}/v1/agents/custom/:agentId`, ({ params }) => {
    requests.push({ path: `archive:${String(params.agentId)}`, method: 'DELETE', body: null })
    return new HttpResponse(null, { status: 204 })
  }),
  http.post(`${BASE}/v1/agents/trigger`, async ({ request }) => {
    requests.push({ path: 'trigger', method: 'POST', body: await request.json() })
    return HttpResponse.json({ run_id: 'run-abc-123', status: 'started' })
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  requests.length = 0
  customAgents = []
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

describe('CustomAgentsPage', () => {
  test('lists the project custom agents', async () => {
    customAgents = [makeCustomAgent()]
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom" element={<CustomAgentsPage />} />
      </Routes>,
      '/agents/custom',
    )
    expect(await screen.findByText('Churn watch')).toBeInTheDocument()
    expect(screen.getByText('churn_watch')).toBeInTheDocument()
    expect(screen.getByText('churn_signals')).toBeInTheDocument()
  })

  test('archives via the confirm dialog', async () => {
    customAgents = [makeCustomAgent()]
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom" element={<CustomAgentsPage />} />
      </Routes>,
      '/agents/custom',
    )
    await userEvent.click(await screen.findByRole('button', { name: 'Archive Churn watch' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Archive' }))
    await waitFor(() =>
      expect(requests.some((entry) => entry.path === 'archive:agent-1')).toBe(true),
    )
  })

  test('shows the empty state with a create link', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom" element={<CustomAgentsPage />} />
      </Routes>,
      '/agents/custom',
    )
    expect(await screen.findByText('No custom agents yet')).toBeInTheDocument()
  })
})

describe('CustomAgentWizardPage', () => {
  async function fillWizardToTestStep() {
    // Step 1: Basics — slug auto-derives from the name.
    await userEvent.type(await screen.findByLabelText('Name'), 'Churn watch')
    expect(screen.getByLabelText('Slug')).toHaveValue('churn_watch')
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Step 2: Preset queries — optional; skip through.
    expect(await screen.findByText('No preset queries', { exact: false })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Step 3: Prompts — template is prefilled; add the system prompt.
    await userEvent.type(
      await screen.findByLabelText('System prompt'),
      'You are a churn analyst.',
    )
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Step 4: Data tools — default allows the whole catalog; limit to one.
    await userEvent.click(await screen.findByRole('radio', { name: /limit tools/i }))
    await userEvent.click(await screen.findByRole('checkbox', { name: /discover_events/ }))
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Step 5: Behavior — output key.
    await userEvent.type(await screen.findByLabelText('Output key (produces)'), 'churn_signals')
    await userEvent.click(screen.getByRole('button', { name: /next/i }))
  }

  test('walks the steps, dry-runs the draft, and creates the agent', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom" element={<div>list page</div>} />
        <Route path="/agents/custom/new" element={<CustomAgentWizardPage />} />
      </Routes>,
      '/agents/custom/new',
    )

    await fillWizardToTestStep()

    // Step 5: dry-run first — the test endpoint gets the full draft.
    await userEvent.click(await screen.findByRole('button', { name: /run test/i }))
    expect(await screen.findByText('Test result')).toBeInTheDocument()
    // Appears in the parsed-output view and again in the raw response block.
    expect(screen.getAllByText(/activation drop/).length).toBeGreaterThanOrEqual(1)
    const testCall = requests.find((entry) => entry.path === 'test')
    expect(testCall?.body).toMatchObject({
      project_id: 'demo',
      definition: { slug: 'churn_watch', produces: 'churn_signals' },
    })

    // Save.
    await userEvent.click(screen.getByRole('button', { name: /create agent/i }))
    expect(await screen.findByText('list page')).toBeInTheDocument()
    const createCall = requests.find((entry) => entry.path === 'create')
    expect(createCall?.body).toMatchObject({
      slug: 'churn_watch',
      display_name: 'Churn watch',
      system_prompt: 'You are a churn analyst.',
      model_tier: 'reasoning',
      tools: ['discover_events'],
      max_tool_steps: 8,
      requires: [],
      produces: 'churn_signals',
      memory_query: null,
      pipeline_order: 100,
    })
  })

  test('builds a preset funnel query with the structured form and sends it in the spec', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom" element={<div>list page</div>} />
        <Route path="/agents/custom/new" element={<CustomAgentWizardPage />} />
      </Routes>,
      '/agents/custom/new',
    )

    // Basics → Preset queries.
    await userEvent.type(await screen.findByLabelText('Name'), 'Churn watch')
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Add a preset (defaults to the first catalog tool) and switch it to a
    // funnel — the form becomes two ordered steps plus a conversion window.
    await userEvent.click(await screen.findByRole('button', { name: /add preset query/i }))
    expect(screen.getByText('Max events')).toBeInTheDocument() // discover_events form
    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Preset query 1 tool' }),
      'query_funnel',
    )

    // Empty step events block Next with per-step problems.
    await userEvent.click(screen.getByRole('button', { name: /next/i }))
    expect(
      await screen.findByText('Preset query 1: step 1 — pick an event.'),
    ).toBeInTheDocument()

    // Pick the step events from the discovered catalog.
    await userEvent.click(screen.getByRole('combobox', { name: 'Step 1 event' }))
    await userEvent.click(await screen.findByRole('button', { name: 'signup' }))
    await userEvent.click(screen.getByRole('combobox', { name: 'Step 2 event' }))
    await userEvent.click(await screen.findByRole('button', { name: 'purchase' }))
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Prompts — the preset makes {tool_results} a documented placeholder.
    expect((await screen.findAllByText(/\{tool_results\}/)).length).toBeGreaterThan(0)
    await userEvent.type(
      await screen.findByLabelText('System prompt'),
      'You are a churn analyst.',
    )
    await userEvent.click(screen.getByRole('button', { name: /next/i }))

    // Data tools → Behavior → save.
    await userEvent.click(await screen.findByRole('radio', { name: /limit tools/i }))
    await userEvent.click(await screen.findByRole('checkbox', { name: /discover_events/ }))
    await userEvent.click(screen.getByRole('button', { name: /next/i }))
    await userEvent.type(await screen.findByLabelText('Output key (produces)'), 'churn_signals')
    await userEvent.click(screen.getByRole('button', { name: /next/i }))
    await userEvent.click(await screen.findByRole('button', { name: /create agent/i }))
    expect(await screen.findByText('list page')).toBeInTheDocument()

    const createCall = requests.find((entry) => entry.path === 'create')
    expect(createCall?.body).toMatchObject({
      slug: 'churn_watch',
      preset_tools: [
        {
          tool: 'query_funnel',
          params: {
            steps: [
              { event_name: 'signup', filters: [] },
              { event_name: 'purchase', filters: [] },
            ],
            window_days: 7,
          },
        },
      ],
    })
  })

  test('blocks Next on an invalid step', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom/new" element={<CustomAgentWizardPage />} />
      </Routes>,
      '/agents/custom/new',
    )
    // No name/slug yet: Next must surface problems and stay on Basics.
    await userEvent.click(await screen.findByRole('button', { name: /next/i }))
    expect(await screen.findByText('Name is required.')).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toBeInTheDocument()
  })

  test('prefills the form when editing an existing agent', async () => {
    customAgents = [makeCustomAgent()]
    renderWithProviders(
      <Routes>
        <Route path="/agents/custom/:agentId/edit" element={<CustomAgentWizardPage />} />
      </Routes>,
      '/agents/custom/agent-1/edit',
    )
    expect(await screen.findByLabelText('Name')).toHaveValue('Churn watch')
    expect(screen.getByLabelText('Slug')).toHaveValue('churn_watch')
  })
})

describe('TriggerPage with custom agents', () => {
  test('merges custom agents into the checkbox list and posts their slug', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/agents/trigger" element={<TriggerPage />} />
        <Route path="/agents/runs/:runId" element={<div>monitor page</div>} />
      </Routes>,
      '/agents/trigger',
    )

    // The custom agent arrives from /definitions with a badge.
    const customLabel = await screen.findByText('Churn watch')
    expect(customLabel).toBeInTheDocument()
    expect(screen.getByText('custom')).toBeInTheDocument()

    // Hand-picking requires custom mode (default runs the built-in loop only).
    await userEvent.click(screen.getByRole('button', { name: 'Custom' }))
    await userEvent.click(screen.getByRole('checkbox', { name: /churn watch/i }))
    await userEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(await screen.findByText('monitor page')).toBeInTheDocument()
    const trigger = requests.find((entry) => entry.path === 'trigger')
    expect(trigger?.body).toMatchObject({
      analysis_types: ['behavior_analysis', 'churn_watch'],
    })
  })
})
