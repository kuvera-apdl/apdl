import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { LoginPage } from '../../src/features/auth/LoginPage'

const API_KEY = 'proj_demo_0123456789abcdef'
const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

function renderLogin() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <MemoryRouter
            initialEntries={[{ pathname: '/login', state: { from: '/dashboard' } }]}
          >
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route path="/dashboard" element={<div>Authenticated dashboard</div>} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

test('validates the key, creates a tab session, and returns to the protected page', async () => {
  let providedKey: string | null = null
  server.use(
    http.get('http://localhost:8081/v1/auth/me', ({ request }) => {
      providedKey = request.headers.get('x-api-key')
      return HttpResponse.json({
        credential_id: 'credential-demo',
        project_id: 'demo',
        roles: ['config:read', 'config:write'],
      })
    }),
  )
  renderLogin()

  await userEvent.type(screen.getByLabelText('API key'), API_KEY)
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

  expect(await screen.findByText('Authenticated dashboard')).toBeInTheDocument()
  expect(providedKey).toBe(API_KEY)
  expect(sessionStorage.getItem('apdl-admin:session')).toContain('credential-demo')
  expect(localStorage.getItem('apdl-admin:workspaces')).toContain(API_KEY)
})

test('shows an authentication error without starting a session', async () => {
  server.use(
    http.get('http://localhost:8081/v1/auth/me', () =>
      HttpResponse.json({ detail: 'Valid API key required' }, { status: 401 }),
    ),
  )
  renderLogin()

  await userEvent.type(screen.getByLabelText('API key'), API_KEY)
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

  expect(
    await screen.findByText('The API key is invalid, expired, or revoked.'),
  ).toBeInTheDocument()
  await waitFor(() => expect(sessionStorage.getItem('apdl-admin:session')).toBeNull())
})
