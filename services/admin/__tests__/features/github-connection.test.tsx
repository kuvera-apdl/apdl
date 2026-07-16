import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { GitHubConnectionCard } from '../../src/features/codegen/GitHubConnectionCard'
import { seedWorkspace } from '../helpers/fixtures'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => localStorage.clear())

function renderCard() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <GitHubConnectionCard />
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('GitHubConnectionCard', () => {
  test('shows the project repository from its verified grant without exposing installation authority', async () => {
    let internalToken: string | null = null
    server.use(
      http.get('*/api/projects/demo/codegen/v1/connections/demo', ({ request }) => {
        internalToken = request.headers.get('x-apdl-internal-token')
        return HttpResponse.json({
          project_id: 'demo',
          grant_id: 'ghg_demo',
          repository_id: 123456,
          repository_full_name: 'acme/widgets',
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
          updated_at: '2026-07-01T10:00:00+00:00',
        })
      }),
    )

    renderCard()

    expect(await screen.findByText('acme/widgets')).toBeInTheDocument()
    expect(screen.getByText('Verified grant')).toBeInTheDocument()
    expect(screen.getByText(/repository #123456/)).toBeInTheDocument()
    expect(screen.queryByText(/installation/i)).not.toBeInTheDocument()
    expect(internalToken).toBeNull()
  })

  test('keeps repository onboarding in the trusted operator workflow', async () => {
    server.use(
      http.get(
        '*/api/projects/demo/codegen/v1/connections/demo',
        () => new HttpResponse(null, { status: 404 }),
      ),
    )

    renderCard()

    expect(await screen.findByText(/operator must authorize the exact GitHub repository ID/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /connect repository/i })).not.toBeInTheDocument()
  })
})
