import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest'

import { AuthProvider } from '../../src/core/auth'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ProjectMembersCard } from '../../src/features/settings/ProjectMembersCard'

const OWNER_ID = '20000000-0000-4000-8000-000000000002'
const MANAGER_ID = '30000000-0000-4000-8000-000000000003'
const INVITATION_ID = '40000000-0000-4000-8000-000000000004'
const OWNER_IDENTITY = {
  user_id: OWNER_ID,
  email: 'owner@example.com',
  projects: [
    {
      project_id: 'demo',
      roles: ['config:read', 'config:write', 'members:manage'],
    },
  ],
}
const AUTHORIZATION = {
  project_id: 'demo',
  creator: {
    user_id: OWNER_ID,
    email: 'owner@example.com',
  },
  ownership: {
    kind: 'human',
    owner_user_id: OWNER_ID,
    owner_email: 'owner@example.com',
  },
  execution_authorization: {
    authorized: true,
    source: 'operator_provisioned',
  },
}
const MEMBERS = {
  members: [
    {
      user_id: OWNER_ID,
      email: 'owner@example.com',
      roles: ['config:read', 'config:write', 'members:manage'],
      active: true,
      is_owner: true,
      joined_at: '2026-07-01T12:00:00Z',
    },
    {
      user_id: MANAGER_ID,
      email: 'manager@example.com',
      roles: ['config:read', 'members:manage'],
      active: true,
      is_owner: false,
      joined_at: '2026-07-20T12:00:00Z',
    },
  ],
  pending_invitations: [],
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  localStorage.clear()
  document.cookie = 'apdl_admin_csrf=; Max-Age=0; Path=/'
})
afterAll(() => server.close())

function baseHandlers(identity: object = OWNER_IDENTITY, authorization: object = AUTHORIZATION) {
  return [
    http.get('*/api/auth/me', () => HttpResponse.json(identity)),
    http.get('*/api/projects/demo/authorization', () => HttpResponse.json(authorization)),
    http.get('*/api/projects/demo/members', () => HttpResponse.json(MEMBERS)),
    http.get('*/api/projects/demo/members/audit', () => HttpResponse.json([])),
    http.get('*/api/projects/demo/ownership/audit', () => HttpResponse.json([])),
  ]
}

function renderMembers() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <WorkspaceProvider>
          <MemoryRouter>
            <ProjectMembersCard />
          </MemoryRouter>
        </WorkspaceProvider>
      </AuthProvider>
    </QueryClientProvider>,
  )
}

test('shows ownership, creator, and read-only execution authorization separately', async () => {
  server.use(...baseHandlers())
  renderMembers()

  expect(await screen.findByText('Authorized')).toBeInTheDocument()
  expect(screen.getByText('Project Authority')).toBeInTheDocument()
  expect(screen.getByText('Owner')).toBeInTheDocument()
  expect(screen.getByText('Created by')).toBeInTheDocument()
  expect(screen.getByText('Agent execution')).toBeInTheDocument()
  expect(screen.getByText('Authorized')).toBeInTheDocument()
  expect(screen.getByText('operator_provisioned')).toBeInTheDocument()
  expect(screen.getByText(/ownership never changes this state/i)).toBeInTheDocument()
  expect(await screen.findByText('manager@example.com')).toBeInTheDocument()
})

test('keeps a reveal-once invitation URL only while its dialog is open', async () => {
  let submitted: unknown = null
  document.cookie = 'apdl_admin_csrf=members-csrf; Path=/'
  server.use(
    ...baseHandlers(),
    http.post('*/api/projects/demo/invitations', async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json(
        {
          invitation_id: INVITATION_ID,
          email: 'invitee@example.com',
          roles: ['config:read'],
          inviter_email: 'owner@example.com',
          expires_at: '2026-08-06T12:00:00Z',
          created_at: '2026-07-30T12:00:00Z',
          invitation_url: `http://localhost/invitations/${'b'.repeat(43)}`,
        },
        { status: 201 },
      )
    }),
  )
  renderMembers()

  await userEvent.click(await screen.findByRole('button', { name: 'Invite member' }))
  await userEvent.type(screen.getByLabelText('Email'), 'Invitee@Example.com')
  await userEvent.click(screen.getByRole('checkbox', { name: /config:read/i }))
  await userEvent.click(screen.getByRole('button', { name: 'Create invitation' }))

  const revealed = await screen.findByLabelText('Invitation URL')
  expect(revealed).toHaveValue(`http://localhost/invitations/${'b'.repeat(43)}`)
  expect(submitted).toEqual({
    email: 'invitee@example.com',
    roles: ['config:read'],
  })
  await userEvent.click(screen.getByRole('button', { name: 'I have saved the invitation' }))
  expect(screen.queryByLabelText('Invitation URL')).not.toBeInTheDocument()

  await userEvent.click(screen.getByRole('button', { name: 'Invite member' }))
  expect(screen.queryByLabelText('Invitation URL')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Create invitation' })).toBeInTheDocument()
})

test('transfers ownership only to an eligible active manager after confirmation', async () => {
  let transferBody: unknown = null
  document.cookie = 'apdl_admin_csrf=members-csrf; Path=/'
  server.use(
    ...baseHandlers(),
    http.post('*/api/projects/demo/ownership/transfer', async ({ request }) => {
      transferBody = await request.json()
      return HttpResponse.json({
        ...AUTHORIZATION,
        ownership: {
          kind: 'human',
          owner_user_id: MANAGER_ID,
          owner_email: 'manager@example.com',
        },
      })
    }),
  )
  renderMembers()

  await userEvent.click(await screen.findByRole('button', { name: 'Transfer ownership' }))
  await userEvent.selectOptions(screen.getByLabelText('Eligible active manager'), MANAGER_ID)
  await userEvent.click(screen.getByRole('button', { name: 'Confirm transfer' }))

  await waitFor(() => expect(transferBody).toEqual({ target_user_id: MANAGER_ID }))
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Confirm transfer' })).not.toBeInTheDocument(),
  )
})

test('read-only and operator-managed projects expose no inert management controls', async () => {
  let managementCalls = 0
  const identity = {
    user_id: OWNER_ID,
    email: 'viewer@example.com',
    projects: [{ project_id: 'demo', roles: ['config:read'] }],
  }
  const authorization = {
    project_id: 'demo',
    creator: null,
    ownership: { kind: 'operator_managed' },
    execution_authorization: { authorized: false, source: null },
  }
  server.use(
    ...baseHandlers(identity, authorization),
    http.get('*/api/projects/demo/members', () => {
      managementCalls += 1
      return HttpResponse.json(MEMBERS)
    }),
  )
  renderMembers()

  expect(await screen.findByText('Operator managed')).toBeInTheDocument()
  expect(screen.getByText('No console claim action is available.')).toBeInTheDocument()
  expect(await screen.findByText(/controls and access history require/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Invite member' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Transfer ownership' })).not.toBeInTheDocument()
  expect(managementCalls).toBe(0)
})
