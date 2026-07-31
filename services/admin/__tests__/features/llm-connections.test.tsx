import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import type { AdminRole, AuthIdentity } from '../../src/api/auth'
import type {
  LlmConnectionDetail,
  LlmConnectionSummary,
  LlmProviderModel,
} from '../../src/api/llmConnections'
import type { ProjectAuthorization } from '../../src/api/members'
import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ProjectLlmConnectionsCard } from '../../src/features/settings/ProjectLlmConnectionsCard'

const OWNER_ID = '20000000-0000-4000-8000-000000000002'
const DELEGATE_ID = '30000000-0000-4000-8000-000000000003'
const RAW_API_KEY = 'sk-project-secret-that-must-not-persist'

const OWNER_AUTHORIZATION: ProjectAuthorization = {
  project_id: 'demo',
  creator: {
    user_id: OWNER_ID,
    email: 'owner@example.com',
  },
  ownership: {
    kind: 'human',
    owner_user_id: OWNER_ID,
    owner_email: 'owner@example.com',
  },
  execution_authorization: {
    authorized: true,
    source: 'operator_provisioned',
  },
}

const OPENAI_MODEL: LlmProviderModel = {
  schema_version: 'llm_provider_model@1',
  provider: 'openai',
  model_id: 'gpt-5.1-mini',
  display_name: 'GPT 5.1 Mini',
  supported_tiers: ['fast', 'reasoning'],
  catalog_version: 'catalog-2026-07',
  data_residency: 'global',
  allowed_data_classifications: ['public', 'internal'],
  pricing_status: 'operator_review_required',
}

function identity(
  userId: string,
  email: string,
  roles: AdminRole[],
): AuthIdentity {
  return {
    user_id: userId,
    email,
    projects: [{ project_id: 'demo', roles }],
  }
}

function connection(
  overrides: Partial<LlmConnectionSummary> = {},
): LlmConnectionSummary {
  return {
    schema_version: 'llm_provider_connection@1',
    project_id: 'demo',
    provider: 'openai',
    version: 3,
    state: 'active',
    catalog_version: 'catalog-2026-07',
    validated_at: '2026-07-30T12:00:00Z',
    created_at: '2026-07-30T12:00:00Z',
    updated_at: '2026-07-30T12:00:00Z',
    revoked_at: null,
    model_count: 1,
    ...overrides,
  }
}

function connectionDetail(
  overrides: Partial<LlmConnectionDetail> = {},
): LlmConnectionDetail {
  return {
    ...connection(),
    models: [OPENAI_MODEL],
    ...overrides,
  }
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  document.cookie = 'apdl_admin_csrf=llm-csrf; Path=/'
})

interface ReadHandlerOptions {
  currentIdentity: AuthIdentity
  getConnections?: () => LlmConnectionSummary[]
  authorization?: ProjectAuthorization
  authorizationStatus?: number
}

function installReadHandlers({
  currentIdentity,
  getConnections = () => [],
  authorization = OWNER_AUTHORIZATION,
  authorizationStatus = 200,
}: ReadHandlerOptions) {
  const calls = {
    authorization: 0,
    list: 0,
    listProjectIds: [] as Array<string | null>,
  }
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json(currentIdentity)),
    http.get('*/api/projects/demo/authorization', () => {
      calls.authorization += 1
      if (authorizationStatus !== 200) {
        return HttpResponse.json(
          { error: 'forbidden', message: 'Ownership is unavailable' },
          { status: authorizationStatus },
        )
      }
      return HttpResponse.json(authorization)
    }),
    http.get(
      '*/api/projects/demo/agents/v1/agents/llm-connections',
      ({ request }) => {
        calls.list += 1
        calls.listProjectIds.push(new URL(request.url).searchParams.get('project_id'))
        return HttpResponse.json({
          schema_version: 'llm_provider_connection_list@1',
          project_id: 'demo',
          connections: getConnections(),
        })
      },
    ),
  )
  return calls
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter>
            <ProjectLlmConnectionsCard />
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
  return { ...rendered, queryClient }
}

function storageValues(storage: Storage): string[] {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index)
    return key === null ? '' : (storage.getItem(key) ?? '')
  })
}

describe('ProjectLlmConnectionsCard', () => {
  test.each([
    {
      label: 'project owner',
      currentIdentity: identity(OWNER_ID, 'owner@example.com', ['members:manage']),
      expectedAuthorizationCalls: 1,
    },
    {
      label: 'dual-role delegate',
      currentIdentity: identity(DELEGATE_ID, 'delegate@example.com', [
        'agents:manage',
        'credentials:manage',
      ]),
      expectedAuthorizationCalls: 0,
    },
  ])(
    'allows a $label to manage connections',
    async ({ currentIdentity, expectedAuthorizationCalls }) => {
      const calls = installReadHandlers({ currentIdentity })
      renderCard()

      expect(await screen.findAllByRole('button', { name: 'Connect' })).toHaveLength(4)
      expect(screen.queryByText(/controls are hidden/i)).not.toBeInTheDocument()
      expect(calls.authorization).toBe(expectedAuthorizationCalls)
      expect(calls.listProjectIds).toEqual(['demo'])
    },
  )

  test('fails closed when ownership cannot be verified for an agents:read member', async () => {
    const calls = installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'viewer@example.com', ['agents:read']),
      getConnections: () => [connection()],
      authorizationStatus: 403,
    })
    renderCard()

    expect(await screen.findByText(/ownership could not be verified, so controls are hidden/i))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'View models' })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', {
        name: /^(Connect|Replace key|Reconnect|Refresh|Revoke)$/,
      }),
    ).not.toBeInTheDocument()
    expect(calls.authorization).toBe(1)
    expect(calls.list).toBe(1)
  })

  test('does not fetch or reveal metadata without agents:read', async () => {
    const calls = installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'restricted@example.com', ['config:read']),
    })
    renderCard()

    expect(await screen.findByText(/Provider connections require/)).toBeInTheDocument()
    expect(screen.getByText('agents:read')).toBeInTheDocument()
    expect(screen.queryByText('OpenAI')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(calls.authorization).toBe(1)
    expect(calls.list).toBe(0)
  })

  test('connects with the exact secret request then clears it without storage or query retention', async () => {
    let connections: LlmConnectionSummary[] = []
    let putRequest:
      | {
          body: unknown
          csrf: string | null
          pathname: string
          search: string
        }
      | undefined
    installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'delegate@example.com', [
        'agents:read',
        'agents:manage',
        'credentials:manage',
      ]),
      getConnections: () => connections,
    })
    server.use(
      http.put(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai',
        async ({ request }) => {
          const url = new URL(request.url)
          putRequest = {
            body: await request.json(),
            csrf: request.headers.get('x-csrf-token'),
            pathname: url.pathname,
            search: url.search,
          }
          connections = [connection({ version: 1 })]
          return HttpResponse.json(connectionDetail({ version: 1 }))
        },
      ),
    )
    const user = userEvent.setup()
    const { queryClient } = renderCard()

    await user.click((await screen.findAllByRole('button', { name: 'Connect' }))[0]!)
    await user.type(screen.getByLabelText('Provider API key'), RAW_API_KEY)
    await user.click(screen.getByRole('button', { name: 'Validate and save' }))

    await waitFor(() =>
      expect(putRequest).toEqual({
        body: {
          project_id: 'demo',
          api_key: RAW_API_KEY,
          version: 0,
        },
        csrf: 'llm-csrf',
        pathname: '/api/projects/demo/agents/v1/agents/llm-connections/openai',
        search: '',
      }),
    )
    const replaceButton = await screen.findByRole('button', { name: 'Replace key' })
    expect(screen.queryByLabelText('Provider API key')).not.toBeInTheDocument()
    expect(storageValues(localStorage).join(' ')).not.toContain(RAW_API_KEY)
    expect(storageValues(sessionStorage).join(' ')).not.toContain(RAW_API_KEY)
    expect(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((query) => query.state.data),
      ),
    ).not.toContain(RAW_API_KEY)

    await user.click(replaceButton)
    expect(screen.getByLabelText('Provider API key')).toHaveValue('')
  })

  test('clears a rejected provider key while keeping the secret-free error actionable', async () => {
    installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'delegate@example.com', [
        'agents:read',
        'agents:manage',
        'credentials:manage',
      ]),
    })
    server.use(
      http.put(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai',
        () =>
          HttpResponse.json(
            {
              detail: {
                code: 'invalid_key',
                message: 'Provider rejected the credential',
              },
            },
            { status: 401 },
          ),
      ),
    )
    const user = userEvent.setup()
    renderCard()

    await user.click((await screen.findAllByRole('button', { name: 'Connect' }))[0]!)
    const apiKey = screen.getByLabelText('Provider API key')
    await user.type(apiKey, RAW_API_KEY)
    await user.click(screen.getByRole('button', { name: 'Validate and save' }))

    expect(await screen.findByText('Provider rejected the credential')).toBeInTheDocument()
    await waitFor(() => expect(apiKey).toHaveValue(''))
    expect(storageValues(localStorage).join(' ')).not.toContain(RAW_API_KEY)
    expect(storageValues(sessionStorage).join(' ')).not.toContain(RAW_API_KEY)
  })

  test('loads the model view and refreshes with the current connection version', async () => {
    let current = connection()
    let modelsRequest: { projectId: string | null; pathname: string } | undefined
    let refreshRequest: unknown
    installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'delegate@example.com', [
        'agents:read',
        'agents:manage',
        'credentials:manage',
      ]),
      getConnections: () => [current],
    })
    server.use(
      http.get(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai/models',
        ({ request }) => {
          const url = new URL(request.url)
          modelsRequest = {
            projectId: url.searchParams.get('project_id'),
            pathname: url.pathname,
          }
          return HttpResponse.json({
            schema_version: 'llm_provider_model_inventory@1',
            project_id: 'demo',
            provider: 'openai',
            connection_version: current.version,
            models: [OPENAI_MODEL],
          })
        },
      ),
      http.post(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai/refresh-models',
        async ({ request }) => {
          refreshRequest = await request.json()
          current = connection({
            version: 4,
            validated_at: '2026-07-30T12:05:00Z',
            updated_at: '2026-07-30T12:05:00Z',
          })
          return HttpResponse.json(
            connectionDetail({
              version: 4,
              validated_at: '2026-07-30T12:05:00Z',
              updated_at: '2026-07-30T12:05:00Z',
            }),
          )
        },
      ),
    )
    const user = userEvent.setup()
    renderCard()

    await user.click(await screen.findByRole('button', { name: 'View models' }))
    const modelsDialog = await screen.findByRole('dialog')
    expect(await within(modelsDialog).findByText('GPT 5.1 Mini')).toBeInTheDocument()
    expect(within(modelsDialog).getByText('gpt-5.1-mini')).toBeInTheDocument()
    expect(within(modelsDialog).getByText('fast')).toBeInTheDocument()
    expect(within(modelsDialog).getByText('reasoning')).toBeInTheDocument()
    expect(
      within(modelsDialog).getByText(/Data residency: global · classifications: public, internal/),
    ).toBeInTheDocument()
    expect(modelsRequest).toEqual({
      projectId: 'demo',
      pathname: '/api/projects/demo/agents/v1/agents/llm-connections/openai/models',
    })

    await user.click(within(modelsDialog).getAllByRole('button', { name: 'Close' })[0]!)
    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    await waitFor(() =>
      expect(refreshRequest).toEqual({
        project_id: 'demo',
        version: 3,
      }),
    )
    expect(await screen.findByText(/Version 4/)).toBeInTheDocument()
  })

  test('trims and sends the required revocation reason with the current version', async () => {
    let current = connection()
    let revokeRequest: unknown
    installReadHandlers({
      currentIdentity: identity(DELEGATE_ID, 'delegate@example.com', [
        'agents:read',
        'agents:manage',
        'credentials:manage',
      ]),
      getConnections: () => [current],
    })
    server.use(
      http.post(
        '*/api/projects/demo/agents/v1/agents/llm-connections/openai/revoke',
        async ({ request }) => {
          revokeRequest = await request.json()
          current = connection({
            version: 4,
            state: 'revoked',
            updated_at: '2026-07-30T12:10:00Z',
            revoked_at: '2026-07-30T12:10:00Z',
            model_count: 0,
          })
          return HttpResponse.json(current)
        },
      ),
    )
    const user = userEvent.setup()
    renderCard()

    await user.click(await screen.findByRole('button', { name: 'Revoke' }))
    const revokeDialog = await screen.findByRole('dialog')
    await user.type(within(revokeDialog).getByLabelText('Reason'), '  routine rotation  ')
    await user.click(
      within(revokeDialog).getByRole('button', { name: 'Revoke connection' }),
    )

    await waitFor(() =>
      expect(revokeRequest).toEqual({
        project_id: 'demo',
        version: 3,
        reason: 'routine rotation',
      }),
    )
    expect(await screen.findByText(/Version 4/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reconnect' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke' })).not.toBeInTheDocument()
  })
})
