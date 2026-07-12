import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { RegisterPage } from '../../src/features/auth/RegisterPage'
import { WorkspaceSettingsPage } from '../../src/features/settings/WorkspaceSettingsPage'

const IDENTITY = {
  user_id: '30000000-0000-4000-8000-000000000003',
  email: 'new-admin@example.com',
  projects: [],
}

const server = setupServer(
  http.get('*/api/auth/me', () =>
    HttpResponse.json({ detail: 'Login required' }, { status: 401 }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function renderRegistration() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter initialEntries={['/register']}>
            <Routes>
              <Route path="/register" element={<RegisterPage />} />
              <Route path="/settings/workspace" element={<WorkspaceSettingsPage />} />
            </Routes>
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

test('registers an email and password with zero projects', async () => {
  let submitted: unknown = null
  server.use(
    http.post('*/api/auth/register', async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json(IDENTITY, { status: 201 })
    }),
  )
  renderRegistration()

  await userEvent.type(screen.getByLabelText('Email'), 'new-admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'a-new-correct-horse-password')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'a-new-correct-horse-password')
  const submit = screen.getByRole('button', { name: 'Create account' })
  await waitFor(() => expect(submit).toBeEnabled())
  await userEvent.click(submit)

  expect(await screen.findByText('No project access yet')).toBeInTheDocument()
  expect(submitted).toEqual({
    email: 'new-admin@example.com',
    password: 'a-new-correct-horse-password',
  })
  expect(localStorage.getItem('apdl-admin:workspaces')).toBeNull()
  expect(sessionStorage.getItem('apdl-admin:session')).toBeNull()
})

test('rejects mismatched passwords without calling registration', async () => {
  let registrationCalled = false
  server.use(
    http.post('*/api/auth/register', () => {
      registrationCalled = true
      return HttpResponse.json(IDENTITY, { status: 201 })
    }),
  )
  renderRegistration()

  await userEvent.type(screen.getByLabelText('Email'), 'new-admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'a-new-correct-horse-password')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'a-different-password')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))

  expect(await screen.findByText('Passwords do not match')).toBeInTheDocument()
  expect(registrationCalled).toBe(false)
})
