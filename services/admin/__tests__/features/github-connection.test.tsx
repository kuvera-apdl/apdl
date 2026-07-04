import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { GitHubConnectionCard } from '../../src/features/codegen/GitHubConnectionCard'
import { seedWorkspace } from '../helpers/fixtures'

function makeConnection(overrides: Record<string, unknown> = {}) {
  return {
    project_id: 'demo',
    installation_id: 42,
    repo: 'acme/widgets',
    default_base_branch: 'main',
    policy: {},
    created_at: '2026-07-01T10:00:00+00:00',
    updated_at: '2026-07-01T10:00:00+00:00',
    ...overrides,
  }
}

function makeAccessibleRepo(overrides: Record<string, unknown> = {}) {
  return {
    repo: 'acme/site',
    installation_id: 777,
    account: 'acme',
    default_branch: 'main',
    private: false,
    ...overrides,
  }
}

const notConnected = () =>
  HttpResponse.json({ detail: "No repo connection for project 'demo'." }, { status: 404 })

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
})

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <GitHubConnectionCard />
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('GitHubConnectionCard', () => {
  test('shows the connected repository with a disconnect action', async () => {
    // No /v1/github/repos handler: the picker listing must not be fetched
    // while connected (onUnhandledRequest: 'error' enforces it).
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', () =>
        HttpResponse.json(makeConnection()),
      ),
    )
    renderCard()
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    const repoLink = screen.getByRole('link', { name: /acme\/widgets/ })
    expect(repoLink).toHaveAttribute('href', 'https://github.com/acme/widgets')
    expect(screen.getByText(/installation #42/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument()
  })

  test('disconnect confirms, calls DELETE, and flips to the connect form', async () => {
    let deleted = false
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', () =>
        deleted ? notConnected() : HttpResponse.json(makeConnection()),
      ),
      http.delete('http://localhost:8084/v1/connections/demo', () => {
        deleted = true
        return new HttpResponse(null, { status: 204 })
      }),
      http.get('http://localhost:8084/v1/github/repos', () => HttpResponse.json([])),
    )
    renderCard()
    await userEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    // Confirmation dialog: nothing is deleted until confirmed.
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Disconnect repository?')).toBeInTheDocument()
    expect(deleted).toBe(false)
    await userEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('Not connected')).toBeInTheDocument()
    expect(deleted).toBe(true)
    expect(await screen.findByRole('button', { name: 'Connect repository' })).toBeInTheDocument()
  })

  test('unconnected project offers the App repos as a picker and connects', async () => {
    let created: Record<string, unknown> | null = null
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', () =>
        created
          ? HttpResponse.json(makeConnection({ installation_id: 777, repo: 'acme/site' }))
          : notConnected(),
      ),
      http.get('http://localhost:8084/v1/github/repos', () =>
        HttpResponse.json([
          makeAccessibleRepo(),
          makeAccessibleRepo({ repo: 'acme/docs', default_branch: 'develop', private: true }),
        ]),
      ),
      http.post('http://localhost:8084/v1/connections', async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(
          makeConnection({ installation_id: 777, repo: 'acme/site' }),
          { status: 201 },
        )
      }),
    )
    renderCard()
    const user = userEvent.setup()
    const picker = await screen.findByLabelText('Repository')
    // Private repos are marked in the options.
    expect(screen.getByRole('option', { name: 'acme/docs (private)' })).toBeInTheDocument()
    await user.selectOptions(picker, 'acme/site')
    await user.click(screen.getByRole('button', { name: 'Connect repository' }))
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    // The picked repo's installation id rides along — no hand-typed id.
    expect(created).toEqual({
      project_id: 'demo',
      installation_id: 777,
      repo: 'acme/site',
      default_base_branch: 'main',
    })
  })

  test('picking a repo pre-fills its default branch', async () => {
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', notConnected),
      http.get('http://localhost:8084/v1/github/repos', () =>
        HttpResponse.json([makeAccessibleRepo({ repo: 'acme/docs', default_branch: 'develop' })]),
      ),
    )
    renderCard()
    const user = userEvent.setup()
    await user.selectOptions(await screen.findByLabelText('Repository'), 'acme/docs')
    expect(screen.getByLabelText('Base branch')).toHaveValue('develop')
  })

  test('falls back to manual slug entry when the repo listing fails', async () => {
    let created: Record<string, unknown> | null = null
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', () =>
        created ? HttpResponse.json(makeConnection({ repo: 'acme/site' })) : notConnected(),
      ),
      http.get('http://localhost:8084/v1/github/repos', () =>
        HttpResponse.json(
          { detail: 'GitHub App ID and private key are required to mint a JWT.' },
          { status: 503 },
        ),
      ),
      http.post('http://localhost:8084/v1/connections', async ({ request }) => {
        created = (await request.json()) as Record<string, unknown>
        return HttpResponse.json(makeConnection({ repo: 'acme/site' }), { status: 201 })
      }),
    )
    renderCard()
    const user = userEvent.setup()
    await user.type(await screen.findByPlaceholderText('owner/name'), 'acme/site')
    await user.click(screen.getByRole('button', { name: 'Connect repository' }))
    expect(await screen.findByText('Connected')).toBeInTheDocument()
    // No installation id in manual mode — the service resolves it from the slug.
    expect(created).toEqual({
      project_id: 'demo',
      repo: 'acme/site',
      default_base_branch: 'main',
    })
  })

  test('empty listing explains the App has no repository access yet', async () => {
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', notConnected),
      http.get('http://localhost:8084/v1/github/repos', () => HttpResponse.json([])),
    )
    renderCard()
    expect(
      await screen.findByText(/has no repository access yet/),
    ).toBeInTheDocument()
    // Manual entry stays available as the escape hatch.
    expect(screen.getByPlaceholderText('owner/name')).toBeInTheDocument()
  })

  test('rejects a malformed repo slug client-side', async () => {
    server.use(
      http.get('http://localhost:8084/v1/connections/demo', notConnected),
      http.get('http://localhost:8084/v1/github/repos', () => HttpResponse.json([])),
    )
    renderCard()
    const user = userEvent.setup()
    await user.type(await screen.findByPlaceholderText('owner/name'), 'not-a-repo')
    await user.click(screen.getByRole('button', { name: 'Connect repository' }))
    expect(await screen.findByText('Format: owner/name')).toBeInTheDocument()
  })
})
