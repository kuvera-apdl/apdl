import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { LoginPage } from '../../src/features/auth/LoginPage'

const IDENTITY = {
  user_id: '20000000-0000-4000-8000-000000000002',
  email: 'admin@example.com',
  projects: [{ project_id: 'demo', roles: ['config:read', 'config:write'] }],
}

const server = setupServer(
  http.get('*/api/auth/me', () =>
    HttpResponse.json({ detail: 'Login required' }, { status: 401 }),
  ),
  http.get('*/api/auth/capabilities', () =>
    HttpResponse.json({ registration_enabled: true }),
  ),
)

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
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <MemoryRouter initialEntries={[{ pathname: '/login', state: { from: '/dashboard' } }]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/dashboard" element={<div>Authenticated dashboard</div>} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

test('advertises registration only when the strict capability is enabled', async () => {
  renderLogin()

  expect(await screen.findByRole('link', { name: 'Create your account' })).toHaveAttribute(
    'href',
    '/register',
  )
})

test('does not advertise registration when the capability is disabled', async () => {
  let checked = false
  server.use(
    http.get('*/api/auth/capabilities', () => {
      checked = true
      return HttpResponse.json({ registration_enabled: false })
    }),
  )
  renderLogin()

  await waitFor(() => expect(checked).toBe(true))
  expect(screen.queryByRole('link', { name: 'Create your account' })).not.toBeInTheDocument()
})

test('authenticates a human session and returns to the protected page', async () => {
  let submitted: unknown = null
  server.use(
    http.post('*/api/auth/login', async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json(IDENTITY)
    }),
  )
  renderLogin()

  await userEvent.type(screen.getByLabelText('Email'), 'admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'correct-password')
  const submit = screen.getByRole('button', { name: 'Sign in' })
  await waitFor(() => expect(submit).toBeEnabled())
  await userEvent.click(submit)

  expect(await screen.findByText('Authenticated dashboard')).toBeInTheDocument()
  expect(submitted).toEqual({ email: 'admin@example.com', password: 'correct-password' })
  expect(sessionStorage.getItem('apdl-admin:session')).toBeNull()
  expect(localStorage.getItem('apdl-admin:workspaces')).toBeNull()
})

test('shows a generic authentication error without creating browser credentials', async () => {
  server.use(
    http.post('*/api/auth/login', () =>
      HttpResponse.json({ detail: 'Invalid email or password' }, { status: 401 }),
    ),
  )
  renderLogin()

  await userEvent.type(screen.getByLabelText('Email'), 'admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'wrong-password')
  const submit = screen.getByRole('button', { name: 'Sign in' })
  await waitFor(() => expect(submit).toBeEnabled())
  await userEvent.click(submit)

  expect(await screen.findByText('Invalid email or password.')).toBeInTheDocument()
  expect(sessionStorage.getItem('apdl-admin:session')).toBeNull()
})

test('shows the canonical throttle countdown and prevents early resubmission', async () => {
  let attempts = 0
  server.use(
    http.post('*/api/auth/login', () => {
      attempts += 1
      return HttpResponse.json(
        {
          error: 'auth_throttled',
          message: 'Too many attempts. Try again later.',
          retry_after_seconds: 2,
        },
        {
          status: 429,
          headers: { 'Retry-After': '2' },
        },
      )
    }),
  )
  renderLogin()

  await userEvent.type(screen.getByLabelText('Email'), 'admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'wrong-password')
  const submit = screen.getByRole('button', { name: 'Sign in' })
  await waitFor(() => expect(submit).toBeEnabled())
  await userEvent.click(submit)

  expect(
    await screen.findByText(
      'Too many attempts from this browser or network. Try again in 2 seconds.',
    ),
  ).toBeInTheDocument()
  expect(submit).toBeDisabled()
  await userEvent.click(submit)
  expect(attempts).toBe(1)

  expect(
    await screen.findByText(
      'Too many attempts from this browser or network. Try again in 1 second.',
      {},
      { timeout: 1_500 },
    ),
  ).toBeInTheDocument()
  await waitFor(() => expect(submit).toBeEnabled(), { timeout: 1_500 })
})
