import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import type { AdminRole, AuthIdentity } from '../../src/api/auth'
import type { LlmConnectionDetail, LlmConnectionSummary } from '../../src/api/llmConnections'
import type { ProjectAuthorization } from '../../src/api/members'
import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ProjectLlmConnectionsCard } from '../../src/features/settings/ProjectLlmConnectionsCard'

const OWNER_ID = '20000000-0000-4000-8000-000000000002'
const DELEGATE_ID = '30000000-0000-4000-8000-000000000003'
const CONNECTION_ID = '10000000-0000-4000-8000-000000000001'
const RAW_KEY = 'sk-project-secret-that-must-not-persist'

const AUTHORIZATION: ProjectAuthorization = {
  project_id: 'demo',
  creator: { user_id: OWNER_ID, email: 'owner@example.com' },
  ownership: {
    kind: 'human',
    owner_user_id: OWNER_ID,
    owner_email: 'owner@example.com',
  },
  execution_authorization: { authorized: true, source: 'operator_provisioned' },
}

function identity(userId: string, roles: AdminRole[]): AuthIdentity {
  return {
    user_id: userId,
    email: `${userId}@example.com`,
    projects: [{ project_id: 'demo', roles }],
  }
}

function summary(overrides: Partial<LlmConnectionSummary> = {}): LlmConnectionSummary {
  return {
    schema_version: 'project_llm_connection@1',
    connection_id: CONNECTION_ID,
    project_id: 'demo',
    provider: 'openai',
    label: 'Production',
    version: 3,
    inventory_version: 4,
    state: 'active',
    consumers: ['agents', 'codegen'],
    validated_at: '2026-07-30T12:00:00Z',
    created_at: '2026-07-30T11:00:00Z',
    updated_at: '2026-07-30T12:00:00Z',
    revoked_at: null,
    model_count: 1,
    ...overrides,
  }
}

function detail(overrides: Partial<LlmConnectionDetail> = {}): LlmConnectionDetail {
  return {
    ...summary(),
    models: [
      {
        schema_version: 'project_llm_provider_model@1',
        model_id: 'gpt-5.4-mini',
      },
    ],
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

function installReads(
  currentIdentity: AuthIdentity,
  getConnections: () => LlmConnectionSummary[] = () => [],
) {
  const calls = { list: 0, authorization: 0 }
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json(currentIdentity)),
    http.get('*/api/projects/demo/authorization', () => {
      calls.authorization += 1
      return HttpResponse.json(AUTHORIZATION)
    }),
    http.get('*/api/projects/demo/llm-vault/v1/llm-connections', ({ request }) => {
      calls.list += 1
      expect(new URL(request.url).searchParams.get('project_id')).toBe('demo')
      return HttpResponse.json({
        schema_version: 'project_llm_connection_list@1',
        project_id: 'demo',
        connections: getConnections(),
      })
    }),
  )
  return calls
}

function renderCard() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return {
    queryClient,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <WorkspaceProvider>
            <MemoryRouter>
              <ProjectLlmConnectionsCard />
            </MemoryRouter>
          </WorkspaceProvider>
        </AuthProvider>
      </QueryClientProvider>,
    ),
  }
}

function storedValues(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index)
    return key === null ? '' : (storage.getItem(key) ?? '')
  }).join(' ')
}

describe('ProjectLlmConnectionsCard', () => {
  test('shows management only to owner or dual-role delegates', async () => {
    const calls = installReads(
      identity(DELEGATE_ID, ['agents:read', 'agents:manage', 'credentials:manage']),
    )
    renderCard()
    expect(await screen.findByRole('button', { name: 'Add connection' })).toBeInTheDocument()
    expect(calls.authorization).toBe(0)
    expect(calls.list).toBe(1)
  })

  test('keeps an agents:read view secret-free and read-only', async () => {
    installReads(identity(DELEGATE_ID, ['agents:read']), () => [summary()])
    renderCard()
    expect(await screen.findByText('Production')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Models' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add connection' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Replace key' })).not.toBeInTheDocument()
  })

  test('creates a shared connection and clears plaintext immediately', async () => {
    let values: LlmConnectionSummary[] = []
    let body: unknown
    installReads(
      identity(DELEGATE_ID, ['agents:read', 'agents:manage', 'credentials:manage']),
      () => values,
    )
    server.use(
      http.post(
        '*/api/projects/demo/llm-vault/v1/llm-connections',
        async ({ request }) => {
          body = await request.json()
          values = [summary()]
          return HttpResponse.json(detail())
        },
      ),
    )
    const user = userEvent.setup()
    const { queryClient } = renderCard()

    await user.click(await screen.findByRole('button', { name: 'Add connection' }))
    await user.selectOptions(screen.getByLabelText('Provider'), 'openai')
    await user.type(screen.getByLabelText('Label'), 'Production')
    await user.type(screen.getByLabelText('Provider API key'), RAW_KEY)
    await user.click(screen.getByRole('button', { name: 'Validate and save' }))

    await waitFor(() =>
      expect(body).toEqual({
        project_id: 'demo',
        provider: 'openai',
        label: 'Production',
        api_key: RAW_KEY,
        consumers: ['agents', 'codegen'],
      }),
    )
    expect(await screen.findByText('Production')).toBeInTheDocument()
    expect(screen.queryByLabelText('Provider API key')).not.toBeInTheDocument()
    expect(storedValues(localStorage)).not.toContain(RAW_KEY)
    expect(storedValues(sessionStorage)).not.toContain(RAW_KEY)
    expect(JSON.stringify(queryClient.getQueryCache().getAll())).not.toContain(RAW_KEY)
  })

  test('loads models and refreshes exact connection authority', async () => {
    let refreshBody: unknown
    installReads(
      identity(DELEGATE_ID, ['agents:read', 'agents:manage', 'credentials:manage']),
      () => [summary()],
    )
    server.use(
      http.get(
        `*/api/projects/demo/llm-vault/v1/llm-connections/${CONNECTION_ID}`,
        () => HttpResponse.json(detail()),
      ),
      http.post(
        `*/api/projects/demo/llm-vault/v1/llm-connections/${CONNECTION_ID}/refresh`,
        async ({ request }) => {
          refreshBody = await request.json()
          return HttpResponse.json(detail({ version: 4, inventory_version: 5 }))
        },
      ),
    )
    const user = userEvent.setup()
    renderCard()

    await user.click(await screen.findByRole('button', { name: 'Models' }))
    expect(await screen.findByText('gpt-5.4-mini')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(refreshBody).toEqual({ project_id: 'demo', version: 3 }))
  })

  test('replaces grants and revokes through connection-id routes', async () => {
    let replaceBody: unknown
    let revokeBody: unknown
    installReads(
      identity(DELEGATE_ID, ['agents:read', 'agents:manage', 'credentials:manage']),
      () => [summary()],
    )
    server.use(
      http.put(
        `*/api/projects/demo/llm-vault/v1/llm-connections/${CONNECTION_ID}`,
        async ({ request }) => {
          replaceBody = await request.json()
          return HttpResponse.json(detail())
        },
      ),
      http.post(
        `*/api/projects/demo/llm-vault/v1/llm-connections/${CONNECTION_ID}/revoke`,
        async ({ request }) => {
          revokeBody = await request.json()
          return HttpResponse.json(
            summary({
              version: 4,
              inventory_version: 5,
              state: 'revoked',
              consumers: [],
              model_count: 0,
              revoked_at: '2026-07-30T13:00:00Z',
            }),
          )
        },
      ),
    )
    const user = userEvent.setup()
    renderCard()

    await user.click(await screen.findByRole('button', { name: 'Replace key' }))
    await user.click(screen.getByLabelText('Codegen'))
    await user.type(screen.getByLabelText('Provider API key'), 'replacement-key')
    await user.click(screen.getByRole('button', { name: 'Validate and save' }))
    await waitFor(() =>
      expect(replaceBody).toMatchObject({
        project_id: 'demo',
        provider: 'openai',
        consumers: ['agents'],
        version: 3,
      }),
    )

    await user.click(await screen.findByRole('button', { name: 'Revoke' }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByLabelText('Reason'), 'Account retired')
    await user.click(within(dialog).getByRole('button', { name: 'Revoke' }))
    await waitFor(() =>
      expect(revokeBody).toEqual({
        project_id: 'demo',
        version: 3,
        reason: 'Account retired',
      }),
    )
  })
})
