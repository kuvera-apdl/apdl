import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, beforeAll, beforeEach, expect, test } from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'
import { RequireAuth } from '../../src/router'

const server = setupServer(
  http.get('*/api/auth/me', () =>
    HttpResponse.json({
      user_id: '20000000-0000-4000-8000-000000000002',
      email: 'admin@example.com',
      projects: [{ project_id: 'demo', roles: ['config:read'] }],
    }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  localStorage.setItem('apdl-admin:workspaces', 'legacy-secret-state')
  sessionStorage.setItem('apdl-admin:session', 'legacy-secret-state')
})

test('a 401 event ends the server-backed session and redirects protected routes', async () => {
  const queryClient = new QueryClient()
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={['/private']}>
          <Routes>
            <Route element={<RequireAuth />}>
              <Route path="/private" element={<div>Private console</div>} />
            </Route>
            <Route path="/login" element={<div>Login route</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('Private console')).toBeInTheDocument()
  expect(localStorage.getItem('apdl-admin:workspaces')).toBeNull()
  expect(sessionStorage.getItem('apdl-admin:session')).toBeNull()

  act(() => window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT)))

  expect(await screen.findByText('Login route')).toBeInTheDocument()
})
