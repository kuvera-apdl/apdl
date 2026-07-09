import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, expect, test } from 'vitest'

import { AUTH_SESSION_KEY, AuthProvider } from '../../src/core/auth'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'
import { WorkspaceProvider } from '../../src/core/workspace'
import { RequireAuth } from '../../src/router'
import { seedWorkspace } from '../helpers/fixtures'

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

test('a 401 event ends the session and redirects protected routes to login', async () => {
  const workspace = seedWorkspace()
  sessionStorage.setItem(
    AUTH_SESSION_KEY,
    JSON.stringify({
      workspaceId: workspace.id,
      apiKey: workspace.apiKey,
      identity: {
        credential_id: 'credential-demo',
        project_id: 'demo',
        roles: ['config:read'],
      },
    }),
  )
  const queryClient = new QueryClient()
  render(
    <WorkspaceProvider>
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
      </QueryClientProvider>
    </WorkspaceProvider>,
  )

  expect(screen.getByText('Private console')).toBeInTheDocument()

  act(() => window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT)))

  expect(await screen.findByText('Login route')).toBeInTheDocument()
  expect(sessionStorage.getItem(AUTH_SESSION_KEY)).toBeNull()
})
