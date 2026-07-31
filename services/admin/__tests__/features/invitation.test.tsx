import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { InvitationPage } from '../../src/features/auth/InvitationPage'

const TOKEN = 'a'.repeat(43)
const INVITATION = {
  status: 'valid',
  project_id: 'invited',
  email: 'invitee@example.com',
  roles: ['config:read'],
  expires_at: '2026-08-06T12:00:00Z',
}
const INVITED_IDENTITY = {
  user_id: '30000000-0000-4000-8000-000000000003',
  email: 'invitee@example.com',
  projects: [{ project_id: 'invited', roles: ['config:read'] }],
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  localStorage.clear()
})
afterAll(() => server.close())

function renderInvitation() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter initialEntries={[`/invitations/${TOKEN}`]}>
            <Routes>
              <Route path="/invitations/:token" element={<InvitationPage />} />
              <Route path="/login" element={<div>Invitation sign in</div>} />
              <Route path="/" element={<div>Invited project overview</div>} />
            </Routes>
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

test('creates an invited account while public registration is disabled', async () => {
  let registrationBody: unknown = null
  server.use(
    http.get('*/api/auth/me', () =>
      HttpResponse.json({ detail: 'Login required' }, { status: 401 }),
    ),
    http.get(`*/api/invitations/${TOKEN}`, () => HttpResponse.json(INVITATION)),
    http.post(`*/api/invitations/${TOKEN}/register`, async ({ request }) => {
      registrationBody = await request.json()
      return HttpResponse.json(INVITED_IDENTITY, { status: 201 })
    }),
  )
  renderInvitation()

  expect(await screen.findByText('Join project invited')).toBeInTheDocument()
  expect(screen.getByText(/works even when public registration is disabled/i)).toBeInTheDocument()
  await userEvent.type(screen.getByLabelText('Password'), 'invited-secure-password')
  await userEvent.type(screen.getByLabelText('Confirm password'), 'invited-secure-password')
  await userEvent.click(screen.getByRole('button', { name: 'Create account and accept' }))

  expect(await screen.findByText('Invited project overview')).toBeInTheDocument()
  expect(registrationBody).toEqual({ password: 'invited-secure-password' })
  await waitFor(() =>
    expect(localStorage.getItem('apdl-admin:active-project')).toBe('invited'),
  )
})

test('matching authenticated user accepts in one action', async () => {
  let accepts = 0
  server.use(
    http.get('*/api/auth/me', () => HttpResponse.json({ ...INVITED_IDENTITY, projects: [] })),
    http.get(`*/api/invitations/${TOKEN}`, () => HttpResponse.json(INVITATION)),
    http.post(`*/api/invitations/${TOKEN}/accept`, () => {
      accepts += 1
      return HttpResponse.json(INVITED_IDENTITY)
    }),
  )
  renderInvitation()

  expect(await screen.findByText(/Signed in as invitee@example\.com/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Accept invitation' }))

  expect(await screen.findByText('Invited project overview')).toBeInTheDocument()
  expect(accepts).toBe(1)
})

test('wrong signed-in email requires an account switch and never attempts acceptance', async () => {
  let accepts = 0
  server.use(
    http.get('*/api/auth/me', () =>
      HttpResponse.json({
        user_id: '40000000-0000-4000-8000-000000000004',
        email: 'other@example.com',
        projects: [{ project_id: 'other', roles: ['config:read'] }],
      }),
    ),
    http.get(`*/api/invitations/${TOKEN}`, () => HttpResponse.json(INVITATION)),
    http.post(`*/api/invitations/${TOKEN}/accept`, () => {
      accepts += 1
      return HttpResponse.json(INVITED_IDENTITY)
    }),
    http.post('*/api/auth/logout', () => new HttpResponse(null, { status: 204 })),
  )
  renderInvitation()

  expect(await screen.findByText(/signed in as other@example.com/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Accept invitation' })).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Sign out and switch account' }))

  expect(await screen.findByText('Invitation sign in')).toBeInTheDocument()
  expect(accepts).toBe(0)
})

test('invalid, expired, revoked, and accepted links share one unavailable state', async () => {
  server.use(
    http.get('*/api/auth/me', () =>
      HttpResponse.json({ detail: 'Login required' }, { status: 401 }),
    ),
    http.get(`*/api/invitations/${TOKEN}`, () =>
      HttpResponse.json({ detail: 'Invitation is unavailable' }, { status: 404 }),
    ),
  )
  renderInvitation()

  expect(await screen.findByText('Invitation unavailable')).toBeInTheDocument()
  expect(
    screen.getByText('This invitation is invalid, expired, revoked, or already accepted.'),
  ).toBeInTheDocument()
})
