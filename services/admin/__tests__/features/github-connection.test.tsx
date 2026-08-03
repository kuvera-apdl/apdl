import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest'

import type { AdminRole, AuthIdentity } from '../../src/api/auth'
import type { RepoConnection } from '../../src/api/types/codegen'
import type { ProjectAuthorization } from '../../src/api/members'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { GitHubConnectionCard } from '../../src/features/codegen/GitHubConnectionCard'

const OWNER_ID = '10000000-0000-4000-8000-000000000001'
const DELEGATE_ID = '20000000-0000-4000-8000-000000000002'
const AUTHORIZATION_ID = '30000000-0000-4000-8000-000000000003'
const FIRST_CANDIDATE_ID = '40000000-0000-4000-8000-000000000004'
const SECOND_CANDIDATE_ID = '50000000-0000-4000-8000-000000000005'

const PROJECT_AUTHORIZATION: ProjectAuthorization = {
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
    email: userId === OWNER_ID ? 'owner@example.com' : 'delegate@example.com',
    projects: [{ project_id: 'demo', roles }],
  }
}

function identityWithProjects(userId: string, projects: AuthIdentity['projects']): AuthIdentity {
  return {
    user_id: userId,
    email: userId === OWNER_ID ? 'owner@example.com' : 'delegate@example.com',
    projects,
  }
}

function connection(repository = 'acme/widgets'): RepoConnection {
  return {
    project_id: 'demo',
    grant_id: 'ghg_demo',
    repository_id: repository === 'acme/widgets' ? 123456 : 987654,
    repository_full_name: repository,
    default_base_branch: 'main',
    tenant_policy: {
      schema_version: 'tenant_codegen_connection_policy@1',
      test_cmd: null,
      gates: {
        max_files: null,
        max_lines: null,
        additional_protected_paths: [],
      },
      runtime_acceptance: {
        schema_version: 'runtime_acceptance_request@1',
        enabled: false,
      },
    },
    created_at: '2026-07-01T10:00:00+00:00',
    updated_at: '2026-08-03T17:00:00+00:00',
  }
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  document.cookie = 'apdl_admin_csrf=github-csrf; Path=/'
})

function installReads(
  currentIdentity: AuthIdentity,
  currentConnection: () => RepoConnection | null = () => null,
) {
  const calls = { authorization: 0, connection: 0 }
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json(currentIdentity)),
    http.get('*/api/projects/demo/authorization', () => {
      calls.authorization += 1
      return HttpResponse.json(PROJECT_AUTHORIZATION)
    }),
    http.get('*/api/projects/demo/codegen/v1/connections/demo', ({ request }) => {
      calls.connection += 1
      expect(request.headers.get('x-apdl-internal-token')).toBeNull()
      const saved = currentConnection()
      return saved ? HttpResponse.json(saved) : new HttpResponse(null, { status: 404 })
    }),
  )
  return calls
}

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location-search">{location.search}</output>
}

function renderCard({
  initialEntry = '/codegen',
  redirectToInstallation = vi.fn(),
}: {
  initialEntry?: string
  redirectToInstallation?: (url: string) => void
} = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return {
    queryClient,
    redirectToInstallation,
    ...render(
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <WorkspaceProvider>
            <MemoryRouter initialEntries={[initialEntry]}>
              <TooltipProvider>
                <GitHubConnectionCard redirectToInstallation={redirectToInstallation} />
                <LocationProbe />
              </TooltipProvider>
            </MemoryRouter>
          </WorkspaceProvider>
        </AuthProvider>
      </QueryClientProvider>,
    ),
  }
}

describe('GitHubConnectionCard', () => {
  test('shows the project repository and lets its owner change it without exposing installation authority', async () => {
    installReads(identity(OWNER_ID, ['agents:read']), () => connection())
    renderCard()

    expect(await screen.findByText('acme/widgets')).toBeInTheDocument()
    expect(screen.getByText('Connected')).toBeInTheDocument()
    expect(screen.getByText(/repository #123456/)).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /change repository/i })).toBeInTheDocument()
    expect(screen.queryByText(/installation #/i)).not.toBeInTheDocument()
  })

  test('retries a failed project-scoped authorization and redirects with the server URL', async () => {
    const user = userEvent.setup()
    installReads(identity(OWNER_ID, ['agents:read']))
    let requestBody: unknown = null
    server.use(
      http.post(
        '*/api/projects/demo/codegen/v1/github/repository-authorizations',
        async ({ request }) => {
          requestBody = await request.json()
          expect(request.headers.get('x-csrf-token')).toBe('github-csrf')
          return HttpResponse.json({
            schema_version: 'github_repository_authorization_start@1',
            authorization_id: AUTHORIZATION_ID,
            installation_url: 'https://github.com/apps/apdl/installations/new?state=opaque',
            expires_at: '2026-08-03T18:00:00Z',
          })
        },
      ),
    )
    const redirectToInstallation = vi.fn()
    renderCard({
      initialEntry: '/codegen?github_repository_error=authorization_failed',
      redirectToInstallation,
    })

    await user.click(await screen.findByRole('button', { name: /try again/i }))

    await waitFor(() => {
      expect(requestBody).toEqual({ project_id: 'demo' })
      expect(redirectToInstallation).toHaveBeenCalledWith(
        'https://github.com/apps/apdl/installations/new?state=opaque',
      )
      expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/)
    })
  })

  test('allows a dual-role delegate without treating the shared App as a global repo list', async () => {
    const calls = installReads(
      identity(DELEGATE_ID, ['agents:read', 'agents:manage', 'credentials:manage']),
    )
    renderCard()

    expect(await screen.findByRole('button', { name: /connect github/i })).toBeInTheDocument()
    expect(calls.authorization).toBe(0)
  })

  test('keeps repository changes read-only for a non-owner without delegated roles', async () => {
    const calls = installReads(identity(DELEGATE_ID, ['agents:read']))
    renderCard()

    expect(
      await screen.findByText(/connection changes require project ownership/i),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /connect github/i })).not.toBeInTheDocument()
    expect(calls.authorization).toBe(1)
  })

  test('loads only callback-scoped candidates, completes by opaque candidate id, and cleans the URL', async () => {
    const user = userEvent.setup()
    let savedConnection: RepoConnection | null = null
    let completeBody: unknown = null
    installReads(identity(OWNER_ID, ['agents:read']), () => savedConnection)
    server.use(
      http.get(
        `*/api/projects/demo/codegen/v1/github/repository-authorizations/${AUTHORIZATION_ID}`,
        ({ request }) => {
          expect(new URL(request.url).searchParams.get('project_id')).toBe('demo')
          return HttpResponse.json({
            schema_version: 'github_repository_authorization@1',
            authorization_id: AUTHORIZATION_ID,
            project_id: 'demo',
            status: 'awaiting_selection',
            repositories: [
              {
                candidate_id: FIRST_CANDIDATE_ID,
                repository_id: 123456,
                repository_full_name: 'acme/widgets',
                default_base_branch: 'main',
                private: true,
              },
              {
                candidate_id: SECOND_CANDIDATE_ID,
                repository_id: 987654,
                repository_full_name: 'octo/selected',
                default_base_branch: 'main',
                private: false,
              },
            ],
            expires_at: '2026-08-03T18:00:00Z',
          })
        },
      ),
      http.post(
        `*/api/projects/demo/codegen/v1/github/repository-authorizations/${AUTHORIZATION_ID}/complete`,
        async ({ request }) => {
          completeBody = await request.json()
          savedConnection = connection('octo/selected')
          return HttpResponse.json(savedConnection)
        },
      ),
    )
    renderCard({
      initialEntry: `/codegen?github_repository_authorization=${AUTHORIZATION_ID}&github_repository_project_id=demo`,
    })

    expect(await screen.findByRole('dialog', { name: /choose a github repository/i })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/))
    await user.click(await screen.findByLabelText(/octo\/selected/i))
    await user.click(screen.getByRole('button', { name: /connect repository/i }))

    await waitFor(() => {
      expect(completeBody).toEqual({
        project_id: 'demo',
        candidate_id: SECOND_CANDIDATE_ID,
      })
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })
    expect(await screen.findByText('octo/selected')).toBeInTheDocument()
    expect(JSON.stringify(completeBody)).not.toContain('installation')
  })

  test('switches to the exact authorized callback project before loading its candidates', async () => {
    let authorizationReads = 0
    installReads(
      identityWithProjects(OWNER_ID, [
        { project_id: 'other', roles: ['agents:read'] },
        { project_id: 'demo', roles: ['agents:read'] },
      ]),
    )
    server.use(
      http.get(
        `*/api/projects/demo/codegen/v1/github/repository-authorizations/${AUTHORIZATION_ID}`,
        ({ request }) => {
          authorizationReads += 1
          expect(new URL(request.url).searchParams.get('project_id')).toBe('demo')
          return HttpResponse.json({
            schema_version: 'github_repository_authorization@1',
            authorization_id: AUTHORIZATION_ID,
            project_id: 'demo',
            status: 'awaiting_selection',
            repositories: [],
            expires_at: '2026-08-03T18:00:00Z',
          })
        },
      ),
    )

    renderCard({
      initialEntry: `/codegen?github_repository_authorization=${AUTHORIZATION_ID}&github_repository_project_id=demo`,
    })

    expect(
      await screen.findByRole('dialog', { name: /choose a github repository/i }),
    ).toBeInTheDocument()
    await waitFor(() => {
      expect(authorizationReads).toBe(1)
      expect(localStorage.getItem('apdl-admin:active-project')).toBe('demo')
      expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/)
    })
  })

  test('shows organization approval-required guidance for the callback project', async () => {
    installReads(identity(OWNER_ID, ['agents:read']))
    renderCard({
      initialEntry:
        '/codegen?github_repository_status=installation_approval_required&github_repository_project_id=demo',
    })

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      'A GitHub organization owner must approve the APDL GitHub App before project demo can connect a repository.',
    )
    expect(
      await screen.findByRole('button', { name: /try again after approval/i }),
    ).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/))
  })

  test('rejects callback project context outside the signed-in workspace list', async () => {
    installReads(identity(OWNER_ID, ['agents:read']))
    renderCard({
      initialEntry: `/codegen?github_repository_authorization=${AUTHORIZATION_ID}&github_repository_project_id=other`,
    })

    expect(
      await screen.findByText('GitHub could not authorize the repository. No connection was changed.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/))
  })

  test('renders only a generic callback failure and removes it when dismissed', async () => {
    const user = userEvent.setup()
    installReads(identity(OWNER_ID, ['agents:read']))
    renderCard({ initialEntry: '/codegen?github_repository_error=authorization_failed' })

    expect(
      await screen.findByRole('alert', { name: '' }),
    ).toHaveTextContent('GitHub could not authorize the repository. No connection was changed.')
    await user.click(screen.getByRole('button', { name: /dismiss/i }))

    await waitFor(() => {
      expect(screen.queryByText(/GitHub could not authorize/)).not.toBeInTheDocument()
      expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/)
    })
  })
})
