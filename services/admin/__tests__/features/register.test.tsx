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
  http.get('*/api/auth/capabilities', () =>
    HttpResponse.json({ registration_enabled: true }),
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

  await userEvent.type(await screen.findByLabelText('Email'), 'new-admin@example.com')
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

  await userEvent.type(await screen.findByLabelText('Email'), 'new-admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'a-new-correct-horse-password')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'a-different-password')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))

  expect(await screen.findByText('Passwords do not match')).toBeInTheDocument()
  expect(registrationCalled).toBe(false)
})

test('keeps the direct registration route closed while capability discovery is loading', async () => {
  let releaseCapability: (() => void) | undefined
  let registrationCalled = false
  server.use(
    http.get('*/api/auth/capabilities', async () => {
      await new Promise<void>((resolve) => {
        releaseCapability = resolve
      })
      return HttpResponse.json({ registration_enabled: true })
    }),
    http.post('*/api/auth/register', () => {
      registrationCalled = true
      return HttpResponse.json(IDENTITY, { status: 201 })
    }),
  )
  renderRegistration()

  expect(await screen.findByRole('status')).toHaveTextContent(
    'Verifying whether this APDL deployment is accepting new accounts.',
  )
  expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
  expect(registrationCalled).toBe(false)

  releaseCapability?.()
  expect(await screen.findByLabelText('Email')).toBeInTheDocument()
})

test('keeps registration closed when the capability is disabled', async () => {
  let registrationCalled = false
  server.use(
    http.get('*/api/auth/capabilities', () =>
      HttpResponse.json({ registration_enabled: false }),
    ),
    http.post('*/api/auth/register', () => {
      registrationCalled = true
      return HttpResponse.json(IDENTITY, { status: 201 })
    }),
  )
  renderRegistration()

  expect(await screen.findByText('Registration is disabled')).toBeInTheDocument()
  expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
  expect(registrationCalled).toBe(false)
})

test('keeps registration closed on a malformed capability and supports retry', async () => {
  let attempts = 0
  server.use(
    http.get('*/api/auth/capabilities', () => {
      attempts += 1
      return HttpResponse.json(
        attempts === 1
          ? { registration_enabled: true, legacy_registration_mode: 'open' }
          : { registration_enabled: true },
      )
    }),
  )
  renderRegistration()

  expect(
    await screen.findByText('Unable to verify registration availability'),
  ).toBeInTheDocument()
  expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Retry' }))

  expect(await screen.findByLabelText('Email')).toBeInTheDocument()
  expect(attempts).toBe(2)
})

test.each([
  {
    code: 'account_exists',
    status: 409,
    expected: 'An account already exists for this email. Sign in instead.',
    formRemains: true,
  },
  {
    code: 'account_capacity_reached',
    status: 409,
    expected: 'Account capacity reached',
    formRemains: false,
  },
  {
    code: 'registration_disabled',
    status: 403,
    expected: 'Registration is disabled',
    formRemains: false,
  },
])('handles canonical registration error $code', async ({ code, status, expected, formRemains }) => {
  server.use(
    http.post('*/api/auth/register', () =>
      HttpResponse.json({ error: code, message: 'Registration rejected.' }, { status }),
    ),
  )
  renderRegistration()

  await userEvent.type(await screen.findByLabelText('Email'), 'new-admin@example.com')
  await userEvent.type(screen.getByLabelText('Password'), 'a-new-correct-horse-password')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'a-new-correct-horse-password')
  await userEvent.click(screen.getByRole('button', { name: 'Create account' }))

  expect(await screen.findByText(expected)).toBeInTheDocument()
  if (formRemains) {
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
  } else {
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
  }
})

test('creates a project from a zero-project workspace and associates it with the profile', async () => {
  let submitted: unknown = null
  let csrfHeader: string | null = null
  const withProject = {
    ...IDENTITY,
    projects: [
      {
        project_id: 'firstproject',
        roles: [
          'events:write',
          'config:read',
          'config:write',
          'config:evaluate',
          'query:read',
          'agents:read',
          'credentials:manage',
        ],
      },
    ],
  }
  document.cookie = 'apdl_admin_csrf=project-csrf; Path=/'
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json(IDENTITY)),
    http.post('*/api/projects', async ({ request }) => {
      submitted = await request.json()
      csrfHeader = request.headers.get('x-csrf-token')
      return HttpResponse.json(withProject, { status: 201 })
    }),
    http.get('*/api/projects/firstproject/credentials', () => HttpResponse.json([])),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter initialEntries={['/settings/workspace']}>
            <Routes>
              <Route path="/settings/workspace" element={<WorkspaceSettingsPage />} />
            </Routes>
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )

  expect(await screen.findByText('No project access yet')).toBeInTheDocument()
  await userEvent.type(screen.getByLabelText('Project ID'), 'firstproject')
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }))

  expect((await screen.findAllByText('firstproject')).length).toBeGreaterThanOrEqual(1)
  expect(screen.queryByText('No project access yet')).not.toBeInTheDocument()
  expect(submitted).toEqual({ project_id: 'firstproject' })
  expect(csrfHeader).toBe('project-csrf')
})

test('reports the canonical project quota error in workspace settings', async () => {
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json(IDENTITY)),
    http.post('*/api/projects', () =>
      HttpResponse.json(
        { error: 'project_quota_reached', message: 'Project quota reached.' },
        { status: 409 },
      ),
    ),
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter initialEntries={['/settings/workspace']}>
            <Routes>
              <Route path="/settings/workspace" element={<WorkspaceSettingsPage />} />
            </Routes>
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )

  await userEvent.type(await screen.findByLabelText('Project ID'), 'secondproject')
  await userEvent.click(screen.getByRole('button', { name: 'Create project' }))

  expect(
    await screen.findByText(
      'This account has reached its project limit. Ask an operator for access.',
    ),
  ).toBeInTheDocument()
})
