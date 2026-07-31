import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
} from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'
import {
  useWorkspace,
  WorkspaceProvider,
} from '../../src/core/workspace'
import { AgenticRunsCard } from '../../src/features/agents/setup/AgenticRunsCard'
import { LlmConnectionsManager } from '../../src/features/agents/setup/LlmConnectionsManager'
import {
  makeAgentsSetup,
  seedWorkspace,
} from '../helpers/fixtures'

const IDENTITY = {
  user_id: '30000000-0000-4000-8000-000000000003',
  email: 'owner@example.com',
  projects: [
    {
      project_id: 'demo',
      roles: [
        'agents:read',
        'credentials:manage',
        'members:manage',
      ],
    },
  ],
}

const MODEL_BASE = {
  schema_version: 'llm_provider_model@1',
  provider: 'openai',
  catalog_version: 'llm-provider-catalog@2',
  data_residency: 'global',
  allowed_data_classifications: [
    'public',
    'internal',
    'confidential',
    'restricted',
  ],
  endpoint_host: 'api.openai.com',
  pricing_status: 'catalog_reviewed',
} as const

const MODELS = [
  {
    ...MODEL_BASE,
    model_id: 'gpt-5.4-mini',
    display_name: 'GPT-5.4 Mini',
    supported_tiers: ['fast', 'reasoning'],
    input_cost_per_million_tokens_usd_micros: 250_000,
    output_cost_per_million_tokens_usd_micros: 1_000_000,
  },
  {
    ...MODEL_BASE,
    model_id: 'o4-mini',
    display_name: 'OpenAI o4-mini',
    supported_tiers: ['reasoning'],
    input_cost_per_million_tokens_usd_micros: 1_100_000,
    output_cost_per_million_tokens_usd_micros: 4_400_000,
  },
]

const CONNECTIONS = {
  schema_version: 'llm_provider_connection_list@1',
  project_id: 'demo',
  connections: [
    {
      schema_version: 'llm_provider_connection@1',
      project_id: 'demo',
      provider: 'openai',
      version: 1,
      inventory_version: 1,
      state: 'active',
      catalog_version: 'llm-provider-catalog@2',
      validated_at: '2026-07-30T12:00:00+00:00',
      created_at: '2026-07-30T12:00:00+00:00',
      updated_at: '2026-07-30T12:00:00+00:00',
      revoked_at: null,
      model_count: 2,
    },
  ],
}

let setup = makeAgentsSetup({
  state: 'inactive',
  version: 0,
  caller_capabilities: {
    can_read: true,
    can_manage: true,
    can_activate: true,
    can_deactivate: false,
    management_authority: 'owner',
  },
  assignments: [],
  connections: [
    {
      provider: 'openai',
      connection_version: 1,
      inventory_version: 1,
      state: 'active',
      catalog_version: 'llm-provider-catalog@2',
      current: true,
      validated_at: '2026-07-30T12:00:00+00:00',
    },
  ],
  blockers: [
    'fast_model_required',
    'project_inactive',
    'reasoning_model_required',
  ],
  analysis_ready: false,
  activated_at: null,
})
let submittedSetup: unknown = null
let submittedDeactivation: unknown = null
let authRequests = 0

const server = setupServer(
  http.get('*/api/auth/me', () => {
    authRequests += 1
    return HttpResponse.json(
      setup.state === 'active'
        ? {
            ...IDENTITY,
            projects: [
              {
                ...IDENTITY.projects[0],
                roles: [
                  ...IDENTITY.projects[0].roles,
                  'agents:run',
                  'agents:manage',
                ],
              },
            ],
          }
        : IDENTITY,
    )
  }),
  http.get('*/api/projects/demo/agents/v1/agents/setup', () =>
    HttpResponse.json(setup),
  ),
  http.put(
    '*/api/projects/demo/agents/v1/agents/setup',
    async ({ request }) => {
      submittedSetup = await request.json()
      setup = makeAgentsSetup()
      return HttpResponse.json(setup)
    },
  ),
  http.post(
    '*/api/projects/demo/agents/v1/agents/setup/deactivate',
    async ({ request }) => {
      submittedDeactivation = await request.json()
      setup = makeAgentsSetup({
        state: 'inactive',
        version: 2,
        caller_capabilities: {
          can_read: true,
          can_manage: true,
          can_activate: true,
          can_deactivate: false,
          management_authority: 'owner',
        },
        blockers: ['project_inactive'],
        analysis_ready: false,
        deactivated_at: '2026-07-30T13:00:00+00:00',
        deactivation_reason: 'Pause autonomous analysis',
      })
      return HttpResponse.json(setup)
    },
  ),
  http.get(
    '*/api/projects/demo/agents/v1/agents/llm-connections',
    () => HttpResponse.json(CONNECTIONS),
  ),
  http.get(
    '*/api/projects/demo/agents/v1/agents/llm-connections/openai/models',
    () =>
      HttpResponse.json({
        schema_version: 'llm_provider_model_inventory@1',
        project_id: 'demo',
        provider: 'openai',
        connection_version: 1,
        inventory_version: 1,
        models: MODELS,
      }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  submittedSetup = null
  submittedDeactivation = null
  authRequests = 0
  setup = makeAgentsSetup({
    state: 'inactive',
    version: 0,
    caller_capabilities: {
      can_read: true,
      can_manage: true,
      can_activate: true,
      can_deactivate: false,
      management_authority: 'owner',
    },
    assignments: [],
    connections: [
      {
        provider: 'openai',
        connection_version: 1,
        inventory_version: 1,
        state: 'active',
        catalog_version: 'llm-provider-catalog@2',
        current: true,
        validated_at: '2026-07-30T12:00:00+00:00',
      },
    ],
    blockers: [
      'fast_model_required',
      'project_inactive',
      'reasoning_model_required',
    ],
    analysis_ready: false,
    activated_at: null,
  })
})

function renderConnections(queryClient: QueryClient) {
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <LlmConnectionsManager canManage />
        </MemoryRouter>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

function ProjectSwitcher() {
  const { setActive } = useWorkspace()
  return (
    <>
      <button type="button" onClick={() => setActive('other')}>
        Switch project
      </button>
      <LlmConnectionsManager canManage />
    </>
  )
}

function RoleProbe() {
  const { active } = useWorkspace()
  return <p data-testid="active-roles">{active?.roles.join(',')}</p>
}

function renderSetupCard(queryClient: QueryClient, autoOpen = false) {
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter>
            <AgenticRunsCard autoOpen={autoOpen} />
            <RoleProbe />
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

async function reachSetupReview(dialog: HTMLElement) {
  await userEvent.click(
    await within(dialog).findByRole('button', {
      name: 'Continue to models',
    }),
  )
  await userEvent.selectOptions(
    within(dialog).getByLabelText('Fast model'),
    'openai\u001fgpt-5.4-mini',
  )
  await userEvent.selectOptions(
    within(dialog).getByLabelText('Reasoning model'),
    'openai\u001fo4-mini',
  )
  await userEvent.click(
    within(dialog).getByRole('button', { name: 'Review setup' }),
  )
}

describe('LLM connection secret handling', () => {
  test('uses an imperative request and clears the provider key after failure', async () => {
    let submittedConnection: unknown = null
    let adminUnauthorized = false
    const markUnauthorized = () => {
      adminUnauthorized = true
    }
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, markUnauthorized)
    server.use(
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections',
        () =>
          HttpResponse.json({
            schema_version: 'llm_provider_connection_list@1',
            project_id: 'demo',
            connections: [],
          }),
      ),
      http.put(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai',
        async ({ request }) => {
          submittedConnection = await request.json()
          return HttpResponse.json(
            {
              detail: {
                code: 'invalid_key',
                message: 'Provider rejected the key',
              },
            },
            { status: 401 },
          )
        },
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderConnections(queryClient)

    await userEvent.click(
      await screen.findByRole('button', { name: 'Add provider' }),
    )
    const input = screen.getByLabelText('OpenAI API key')
    await userEvent.type(input, 'provider-secret-value')
    await userEvent.click(
      screen.getByRole('button', { name: 'Validate and connect' }),
    )

    expect(
      await screen.findByText('The provider rejected this API key.'),
    ).toBeInTheDocument()
    expect(input).toHaveValue('')
    expect(submittedConnection).toEqual({
      project_id: 'demo',
      api_key: 'provider-secret-value',
      version: 0,
    })
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0)
    expect(adminUnauthorized).toBe(false)
    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, markUnauthorized)
  })

  test('clears a provider key on provider change, cancel, and unmount', async () => {
    server.use(
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections',
        () =>
          HttpResponse.json({
            schema_version: 'llm_provider_connection_list@1',
            project_id: 'demo',
            connections: [],
          }),
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const rendered = renderConnections(queryClient)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Add provider' }),
    )
    const firstInput = screen.getByLabelText('OpenAI API key')
    await userEvent.type(firstInput, 'first-secret')
    await userEvent.selectOptions(
      screen.getByLabelText('Provider'),
      'anthropic',
    )
    expect(screen.getByLabelText('Anthropic API key')).toHaveValue('')

    const secondInput = screen.getByLabelText('Anthropic API key')
    await userEvent.type(secondInput, 'second-secret')
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(secondInput).toHaveValue('')

    await userEvent.click(
      screen.getByRole('button', { name: 'Add provider' }),
    )
    const unmountedInput = screen.getByLabelText('Anthropic API key')
    await userEvent.type(unmountedInput, 'unmount-secret')
    rendered.unmount()
    expect(unmountedInput).toHaveValue('')
  })

  test('clears the key at PUT settlement and aborts a pending PUT on unmount', async () => {
    let settleRequest: (() => void) | null = null
    let requestAborted = false
    server.use(
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections',
        () =>
          HttpResponse.json({
            schema_version: 'llm_provider_connection_list@1',
            project_id: 'demo',
            connections: [],
          }),
      ),
      http.put(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai',
        async ({ request }) => {
          request.signal.addEventListener('abort', () => {
            requestAborted = true
          })
          await new Promise<void>((resolve) => {
            settleRequest = resolve
          })
          return HttpResponse.json({
            ...CONNECTIONS.connections[0],
            models: MODELS,
          })
        },
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const rendered = renderConnections(queryClient)
    await userEvent.click(
      await screen.findByRole('button', { name: 'Add provider' }),
    )
    const input = screen.getByLabelText('OpenAI API key')
    await userEvent.type(input, 'pending-secret')
    await userEvent.click(
      screen.getByRole('button', { name: 'Validate and connect' }),
    )
    await waitFor(() => expect(settleRequest).not.toBeNull())
    rendered.unmount()
    expect(input).toHaveValue('')
    await waitFor(() => expect(requestAborted).toBe(true))
    ;(settleRequest as (() => void) | null)?.()
  })

  test('clears and closes the key form when the active project changes', async () => {
    server.use(
      http.get(
        '*/api/projects/:projectId/agents/v1/agents/llm-connections',
        ({ params }) =>
          HttpResponse.json({
            schema_version: 'llm_provider_connection_list@1',
            project_id: String(params.projectId),
            connections: [],
          }),
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <WorkspaceProvider
        initialWorkspaces={[
          seedWorkspace(),
          {
            ...seedWorkspace(),
            id: 'other',
            name: 'other',
            projectId: 'other',
          },
        ]}
      >
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <ProjectSwitcher />
          </MemoryRouter>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )
    const switchButton = screen.getByRole('button', {
      name: 'Switch project',
    })
    await userEvent.click(
      await screen.findByRole('button', { name: 'Add provider' }),
    )
    const input = screen.getByLabelText('OpenAI API key')
    await userEvent.type(input, 'project-secret')
    fireEvent.click(switchButton)

    await waitFor(() => expect(input).toHaveValue(''))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('Agentic runs setup wizard', () => {
  test('disables model progression for a stale active connection', async () => {
    setup = makeAgentsSetup({
      state: 'inactive',
      version: 0,
      caller_capabilities: {
        can_read: true,
        can_manage: true,
        can_activate: true,
        can_deactivate: false,
        management_authority: 'owner',
      },
      assignments: [],
      connections: [
        {
          provider: 'openai',
          connection_version: 1,
          inventory_version: 1,
          state: 'active',
          catalog_version: 'llm-provider-catalog@2',
          current: false,
          validated_at: '2026-07-30T12:00:00+00:00',
        },
      ],
      blockers: [
        'connection_stale',
        'fast_model_required',
        'project_inactive',
        'reasoning_model_required',
      ],
      analysis_ready: false,
      activated_at: null,
    })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderSetupCard(queryClient, true)

    const dialog = await screen.findByRole('dialog')
    expect(
      await within(dialog).findByText(
        'Add or refresh a current active connection before continuing.',
      ),
    ).toBeVisible()
    expect(
      within(dialog).getByRole('button', { name: 'Continue to models' }),
    ).toBeDisabled()
  })

  test('activates with the exact selected connection and inventory versions', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderSetupCard(queryClient, true)

    const dialog = await screen.findByRole('dialog')
    await reachSetupReview(dialog)
    await userEvent.click(
      within(dialog).getByRole('button', {
        name: 'Activate Agentic runs',
      }),
    )

    await waitFor(() =>
      expect(submittedSetup).toEqual({
        project_id: 'demo',
        fast_model: {
          provider: 'openai',
          model: 'gpt-5.4-mini',
          connection_version: 1,
          inventory_version: 1,
        },
        reasoning_model: {
          provider: 'openai',
          model: 'o4-mini',
          connection_version: 1,
          inventory_version: 1,
        },
        version: 0,
      }),
    )
    await waitFor(() => {
      expect(authRequests).toBeGreaterThanOrEqual(2)
      expect(screen.getByTestId('active-roles')).toHaveTextContent('agents:run')
      expect(screen.getByTestId('active-roles')).toHaveTextContent(
        'agents:manage',
      )
      expect(screen.getByTestId('active-roles')).not.toHaveTextContent(
        'agents:approve',
      )
    })
  })

  test('allows fast and reasoning assignments from different current providers', async () => {
    const anthropicConnection = {
      schema_version: 'llm_provider_connection@1',
      project_id: 'demo',
      provider: 'anthropic',
      version: 2,
      inventory_version: 3,
      state: 'active',
      catalog_version: 'llm-provider-catalog@2',
      validated_at: '2026-07-30T12:05:00+00:00',
      created_at: '2026-07-30T12:05:00+00:00',
      updated_at: '2026-07-30T12:05:00+00:00',
      revoked_at: null,
      model_count: 1,
    } as const
    setup = makeAgentsSetup({
      state: 'inactive',
      version: 0,
      caller_capabilities: {
        can_read: true,
        can_manage: true,
        can_activate: true,
        can_deactivate: false,
        management_authority: 'owner',
      },
      assignments: [],
      connections: [
        ...setup.connections,
        {
          provider: 'anthropic',
          connection_version: 2,
          inventory_version: 3,
          state: 'active',
          catalog_version: 'llm-provider-catalog@2',
          current: true,
          validated_at: '2026-07-30T12:05:00+00:00',
        },
      ],
      blockers: [
        'fast_model_required',
        'project_inactive',
        'reasoning_model_required',
      ],
      analysis_ready: false,
      activated_at: null,
    })
    server.use(
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections',
        () =>
          HttpResponse.json({
            ...CONNECTIONS,
            connections: [
              ...CONNECTIONS.connections,
              anthropicConnection,
            ],
          }),
      ),
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections/anthropic/models',
        () =>
          HttpResponse.json({
            schema_version: 'llm_provider_model_inventory@1',
            project_id: 'demo',
            provider: 'anthropic',
            connection_version: 2,
            inventory_version: 3,
            models: [
              {
                ...MODEL_BASE,
                provider: 'anthropic',
                model_id: 'claude-sonnet-4-6',
                display_name: 'Claude Sonnet 4.6',
                supported_tiers: ['fast', 'reasoning'],
                endpoint_host: 'api.anthropic.com',
                input_cost_per_million_tokens_usd_micros: 3_000_000,
                output_cost_per_million_tokens_usd_micros: 15_000_000,
              },
            ],
          }),
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderSetupCard(queryClient, true)

    const dialog = await screen.findByRole('dialog')
    await userEvent.click(
      await within(dialog).findByRole('button', {
        name: 'Continue to models',
      }),
    )
    const fast = within(dialog).getByLabelText('Fast model')
    const reasoning = within(dialog).getByLabelText('Reasoning model')
    await waitFor(() => expect(fast).toBeEnabled())
    await userEvent.selectOptions(fast, 'openai\u001fgpt-5.4-mini')
    await userEvent.selectOptions(
      reasoning,
      'anthropic\u001fclaude-sonnet-4-6',
    )
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Review setup' }),
    )
    await userEvent.click(
      within(dialog).getByRole('button', {
        name: 'Activate Agentic runs',
      }),
    )

    await waitFor(() =>
      expect(submittedSetup).toEqual({
        project_id: 'demo',
        fast_model: {
          provider: 'openai',
          model: 'gpt-5.4-mini',
          connection_version: 1,
          inventory_version: 1,
        },
        reasoning_model: {
          provider: 'anthropic',
          model: 'claude-sonnet-4-6',
          connection_version: 2,
          inventory_version: 3,
        },
        version: 0,
      }),
    )
  })

  test('returns to model review after an optimistic activation conflict', async () => {
    server.use(
      http.put(
        '*/api/projects/demo/agents/v1/agents/setup',
        () =>
          HttpResponse.json(
            { detail: 'The Agents setup version changed' },
            { status: 409 },
          ),
      ),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderSetupCard(queryClient, true)

    const dialog = await screen.findByRole('dialog')
    await reachSetupReview(dialog)
    await userEvent.click(
      within(dialog).getByRole('button', {
        name: 'Activate Agentic runs',
      }),
    )

    expect(
      await within(dialog).findByText(
        'Setup changed in another session. Model inventories were refreshed; review the selections again.',
      ),
    ).toBeInTheDocument()
    expect(within(dialog).getByLabelText('Fast model')).toBeInTheDocument()
    expect(authRequests).toBe(1)
  })

  test('requires a reason and explicit confirmation before deactivation', async () => {
    setup = makeAgentsSetup()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    renderSetupCard(queryClient)

    await userEvent.click(
      await screen.findByRole('button', { name: 'Deactivate' }),
    )
    const submit = screen.getByRole('button', {
      name: 'Deactivate Agentic runs',
    })
    await userEvent.type(
      screen.getByLabelText('Reason'),
      'Pause autonomous analysis',
    )
    expect(submit).toBeDisabled()
    await userEvent.click(
      screen.getByLabelText(/I understand this immediately blocks/i),
    )
    expect(submit).toBeEnabled()
    await userEvent.click(submit)

    await waitFor(() =>
      expect(submittedDeactivation).toEqual({
        project_id: 'demo',
        version: 1,
        reason: 'Pause autonomous analysis',
      }),
    )
  })
})
