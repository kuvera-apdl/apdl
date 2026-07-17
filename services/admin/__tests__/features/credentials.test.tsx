import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import type {
  CredentialAuditEntry,
  CredentialMetadata,
} from '../../src/api/credentials'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { queryKeys } from '../../src/core/queryClient'
import { WorkspaceProvider, type Workspace } from '../../src/core/workspace'
import { ProjectCredentialsCard } from '../../src/features/settings/ProjectCredentialsCard'
import { makeWorkspace } from '../helpers/fixtures'

const CREATE_SECRET = 'client_demo_1234567890abcdef1234567890abcdef1234567890abcdef'
const ROTATE_SECRET = 'client_demo_fedcba0987654321fedcba0987654321fedcba0987654321'
const FIRST_CREDENTIAL_ID = `managed-${'1'.repeat(32)}`
const SECOND_CREDENTIAL_ID = `managed-${'2'.repeat(32)}`

function makeCredential(overrides: Partial<CredentialMetadata> = {}): CredentialMetadata {
  return {
    credential_id: FIRST_CREDENTIAL_ID,
    project_id: 'demo',
    credential_kind: 'browser',
    key_prefix: 'client_demo_',
    roles: ['events:write', 'config:read'],
    active: true,
    created_at: '2026-07-16T12:00:00+00:00',
    revoked_at: null,
    rotated_from_credential_id: null,
    ...overrides,
  }
}

function makeAuditEntry(overrides: Partial<CredentialAuditEntry> = {}): CredentialAuditEntry {
  return {
    audit_id: '10000000-0000-4000-8000-000000000001',
    project_id: 'demo',
    credential_id: FIRST_CREDENTIAL_ID,
    action: 'create',
    actor_user_id: '20000000-0000-4000-8000-000000000002',
    actor_email: 'owner@example.com',
    credential_kind: 'browser',
    roles: ['events:write', 'config:read'],
    successor_credential_id: null,
    created_at: '2026-07-16T12:00:00+00:00',
    ...overrides,
  }
}

const server = setupServer()

let credentials: CredentialMetadata[] = []
let auditEntries: CredentialAuditEntry[] = []
let listCalls = 0
let createBody: unknown = null
let rotateBody: unknown = null
let revokeBody: unknown = null

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  document.cookie = 'apdl_admin_csrf=credential-csrf; Path=/'
  credentials = [makeCredential()]
  auditEntries = [
    makeAuditEntry(),
    makeAuditEntry({
      audit_id: '10000000-0000-4000-8000-000000000002',
      action: 'rotate',
      successor_credential_id: SECOND_CREDENTIAL_ID,
      created_at: '2026-07-16T12:05:00+00:00',
    }),
    makeAuditEntry({
      audit_id: '10000000-0000-4000-8000-000000000003',
      action: 'revoke',
      created_at: '2026-07-16T12:10:00+00:00',
    }),
  ]
  listCalls = 0
  createBody = null
  rotateBody = null
  revokeBody = null

  server.use(
    http.get('*/api/projects/demo/credentials', () => {
      listCalls += 1
      return HttpResponse.json(credentials)
    }),
    http.post('*/api/projects/demo/credentials', async ({ request }) => {
      createBody = await request.json()
      const created = makeCredential({ credential_id: SECOND_CREDENTIAL_ID })
      credentials = [...credentials, created]
      return HttpResponse.json({ ...created, api_key: CREATE_SECRET }, { status: 201 })
    }),
    http.post(
      '*/api/projects/demo/credentials/:credentialId/rotate',
      async ({ params, request }) => {
        rotateBody = await request.json()
        const predecessor = credentials.find(
          (credential) => credential.credential_id === params.credentialId,
        )!
        const successor = makeCredential({
          credential_id: SECOND_CREDENTIAL_ID,
          credential_kind: predecessor.credential_kind,
          key_prefix: predecessor.key_prefix,
          roles: [...predecessor.roles],
          rotated_from_credential_id: predecessor.credential_id,
          created_at: '2026-07-16T12:05:00+00:00',
        })
        credentials = [...credentials, successor]
        return HttpResponse.json({ ...successor, api_key: ROTATE_SECRET }, { status: 201 })
      },
    ),
    http.post(
      '*/api/projects/demo/credentials/:credentialId/revoke',
      async ({ params, request }) => {
        revokeBody = await request.json()
        let revoked: CredentialMetadata | null = null
        credentials = credentials.map((credential) => {
          if (credential.credential_id !== params.credentialId) return credential
          revoked = {
            ...credential,
            active: false,
            revoked_at: '2026-07-16T12:10:00+00:00',
          }
          return revoked
        })
        return HttpResponse.json(revoked)
      },
    ),
    http.get('*/api/projects/demo/credentials/:credentialId/audit', () =>
      HttpResponse.json(auditEntries),
    ),
  )
})

function storageValues(storage: Storage): string[] {
  return Array.from({ length: storage.length }, (_, index) => {
    const key = storage.key(index)
    return key === null ? '' : (storage.getItem(key) ?? '')
  })
}

function renderCard(workspace: Workspace = makeWorkspace()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const rendered = render(
    <WorkspaceProvider initialWorkspaces={[workspace]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <ProjectCredentialsCard />
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
  return { ...rendered, queryClient }
}

describe('ProjectCredentialsCard', () => {
  test('fails closed without credentials:manage and does not fetch metadata', async () => {
    renderCard(
      makeWorkspace({
        roles: ['events:write', 'config:read', 'config:evaluate', 'query:read'],
      }),
    )

    expect(screen.getByText(/does not grant/i)).toBeInTheDocument()
    expect(screen.getByText('credentials:manage')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create credential' })).not.toBeInTheDocument()
    await waitFor(() => expect(listCalls).toBe(0))
  })

  test('does not offer credential scopes missing from the current membership', async () => {
    const user = userEvent.setup()
    renderCard(
      makeWorkspace({
        roles: ['config:read', 'config:evaluate', 'credentials:manage'],
      }),
    )

    await user.click(screen.getByRole('button', { name: 'Create credential' }))
    const createDialog = await screen.findByRole('dialog')
    expect(
      within(createDialog).getByText(/must include both fixed browser roles/i),
    ).toBeInTheDocument()
    expect(
      within(createDialog).getByRole('button', { name: 'Create and reveal' }),
    ).toBeDisabled()

    await user.selectOptions(within(createDialog).getByLabelText('Credential type'), 'confidential')
    expect(within(createDialog).getByRole('checkbox', { name: /events:write/i })).toBeDisabled()
    expect(within(createDialog).getByRole('checkbox', { name: /query:read/i })).toBeDisabled()
    expect(within(createDialog).getByRole('checkbox', { name: /config:read/i })).toBeEnabled()
    expect(
      within(createDialog).getByRole('checkbox', { name: /config:evaluate/i }),
    ).toBeEnabled()

    await user.click(
      within(createDialog).getByRole('checkbox', { name: /config:evaluate/i }),
    )
    expect(
      within(createDialog).getByRole('button', { name: 'Create and reveal' }),
    ).toBeEnabled()
  })

  test('creates an exact-scope browser key and clears its reveal without caching it', async () => {
    credentials = []
    const user = userEvent.setup()
    const { queryClient } = renderCard()

    await user.click(screen.getByRole('button', { name: 'Create credential' }))
    const createDialog = await screen.findByRole('dialog')
    expect(within(createDialog).getByText('events:write')).toBeInTheDocument()
    expect(within(createDialog).getByText('config:read')).toBeInTheDocument()
    await user.click(within(createDialog).getByRole('button', { name: 'Create and reveal' }))

    expect(await screen.findByDisplayValue(CREATE_SECRET)).toBeInTheDocument()
    expect(createBody).toEqual({
      credential_kind: 'browser',
      roles: ['events:write', 'config:read'],
    })
    expect(storageValues(localStorage).join(' ')).not.toContain(CREATE_SECRET)
    expect(storageValues(sessionStorage).join(' ')).not.toContain(CREATE_SECRET)
    expect(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((query) => query.state.data),
      ),
    ).not.toContain(CREATE_SECRET)

    await user.click(screen.getByRole('button', { name: 'I have saved the key' }))
    await waitFor(() =>
      expect(screen.queryByDisplayValue(CREATE_SECRET)).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(queryClient.getQueryData(queryKeys.credentials('demo'))).toEqual([
        makeCredential({ credential_id: SECOND_CREDENTIAL_ID }),
      ]),
    )
  })

  test('drops an open reveal when the credential component unmounts', async () => {
    credentials = []
    const user = userEvent.setup()
    const { queryClient, unmount } = renderCard()

    await user.click(screen.getByRole('button', { name: 'Create credential' }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Create and reveal',
      }),
    )
    expect(await screen.findByDisplayValue(CREATE_SECRET)).toBeInTheDocument()

    unmount()

    expect(screen.queryByDisplayValue(CREATE_SECRET)).not.toBeInTheDocument()
    expect(JSON.stringify(queryClient.getQueryData(queryKeys.credentials('demo')))).not.toContain(
      CREATE_SECRET,
    )
    expect(storageValues(localStorage).join(' ')).not.toContain(CREATE_SECRET)
    expect(storageValues(sessionStorage).join(' ')).not.toContain(CREATE_SECRET)
  })

  test('rotates with an empty body, reveals the successor, and leaves the predecessor active', async () => {
    const user = userEvent.setup()
    renderCard()

    await user.click(
      await screen.findByRole('button', { name: `Rotate ${FIRST_CREDENTIAL_ID}` }),
    )
    const confirmation = await screen.findByRole('dialog')
    expect(within(confirmation).getByText(/current credential remains active/i)).toBeInTheDocument()
    await user.click(within(confirmation).getByRole('button', { name: 'Create successor' }))

    expect(await screen.findByDisplayValue(ROTATE_SECRET)).toBeInTheDocument()
    expect(rotateBody).toEqual({})
    expect(credentials.find((item) => item.credential_id === FIRST_CREDENTIAL_ID)?.active).toBe(true)
    expect(credentials.find((item) => item.credential_id === SECOND_CREDENTIAL_ID)).toMatchObject({
      rotated_from_credential_id: FIRST_CREDENTIAL_ID,
      active: true,
    })

    await user.click(screen.getByRole('button', { name: 'I have saved the key' }))
    expect(
      await screen.findByRole('button', { name: `Revoke ${FIRST_CREDENTIAL_ID}` }),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: `Rotate ${FIRST_CREDENTIAL_ID}` }),
    ).not.toBeInTheDocument()
    expect(screen.getByText('rotated from')).toBeInTheDocument()
  })

  test('revokes with an empty body and exposes immutable audit history', async () => {
    const user = userEvent.setup()
    renderCard()

    await user.click(
      await screen.findByRole('button', { name: `Revoke ${FIRST_CREDENTIAL_ID}` }),
    )
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', {
        name: 'Revoke credential',
      }),
    )

    expect(revokeBody).toEqual({})
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: `Revoke ${FIRST_CREDENTIAL_ID}` }),
      ).not.toBeInTheDocument(),
    )
    expect(screen.getByText('Revoked')).toBeInTheDocument()

    await user.click(
      screen.getByRole('button', { name: `View audit for ${FIRST_CREDENTIAL_ID}` }),
    )
    const auditDialog = await screen.findByRole('dialog')
    expect(await within(auditDialog).findByText('rotate')).toBeInTheDocument()
    expect(within(auditDialog).getByText(SECOND_CREDENTIAL_ID)).toBeInTheDocument()
    expect(within(auditDialog).getAllByText('owner@example.com').length).toBeGreaterThanOrEqual(2)
  })
})
