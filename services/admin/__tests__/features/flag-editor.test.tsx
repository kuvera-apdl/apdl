import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import type { FlagConfig } from '../../src/api/types/flags'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { FlagEditorPage } from '../../src/features/flags/editor/FlagEditorPage'
import { makeFlag, seedWorkspace } from '../helpers/fixtures'

let currentFlags: FlagConfig[] = []
const putBodies: Record<string, unknown>[] = []
const postBodies: Record<string, unknown>[] = []

const server = setupServer(
  http.get('http://localhost:8081/v1/admin/flags', () =>
    HttpResponse.json({ flags: currentFlags, count: currentFlags.length }),
  ),
  http.post('http://localhost:8081/v1/admin/flags', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    postBodies.push(body)
    if (currentFlags.some((flag) => flag.key === body.key)) {
      return HttpResponse.json(
        { error: 'conflict', message: `Flag with key '${String(body.key)}' already exists` },
        { status: 409 },
      )
    }
    return HttpResponse.json(
      { created: true, flag: makeFlag({ key: String(body.key), name: String(body.name), state: 'draft', enabled: false, version: 1 }) },
      { status: 201 },
    )
  }),
  http.put('http://localhost:8081/v1/admin/flags/:key', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    putBodies.push(body)
    const current = currentFlags[0]!
    if (body.version !== current.version) {
      return HttpResponse.json(
        {
          error: 'version_conflict',
          message: `Flag 'checkout-cta' is at version ${current.version}`,
          current_version: current.version,
        },
        { status: 409 },
      )
    }
    const updated = { ...current, ...(body.name ? { name: String(body.name) } : {}), version: current.version + 1 }
    currentFlags = [updated]
    return HttpResponse.json({ updated: true, flag: updated })
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  currentFlags = []
  putBodies.length = 0
  postBodies.length = 0
})

function renderEditor(initialPath: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // The editor uses useBlocker → needs a data router.
  const router = createMemoryRouter(
    [
      { path: '/flags/new', element: <FlagEditorPage /> },
      { path: '/flags/:key/edit', element: <FlagEditorPage /> },
      { path: '/flags/:key', element: <div>detail page</div> },
      { path: '/flags', element: <div>list page</div> },
    ],
    { initialEntries: [initialPath] },
  )
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('FlagEditorPage — create', () => {
  test('creates a draft flag through the review sheet', async () => {
    renderEditor('/flags/new')
    await userEvent.type(await screen.findByPlaceholderText('checkout-cta'), 'brand-new')
    await userEvent.type(screen.getByPlaceholderText('Checkout CTA experiment'), 'Brand new flag')
    await userEvent.click(screen.getByRole('button', { name: 'Review & save' }))

    // Review sheet shows the canonical payload before anything is sent.
    expect(await screen.findByText('Create brand-new')).toBeInTheDocument()
    expect(postBodies).toHaveLength(0)
    await userEvent.click(screen.getByRole('button', { name: 'Create flag' }))

    expect(await screen.findByText('detail page')).toBeInTheDocument()
    expect(postBodies).toHaveLength(1)
    expect(postBodies[0]).toMatchObject({
      key: 'brand-new',
      name: 'Brand new flag',
      state: 'draft',
      enabled: false,
      default_variant: 'control',
    })
  })

  test('renders a duplicate-key 409 on the key field', async () => {
    currentFlags = [makeFlag({ key: 'taken-key' })]
    renderEditor('/flags/new')
    await userEvent.type(await screen.findByPlaceholderText('checkout-cta'), 'taken-key')
    await userEvent.type(screen.getByPlaceholderText('Checkout CTA experiment'), 'Dup')
    await userEvent.click(screen.getByRole('button', { name: 'Review & save' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Create flag' }))

    expect(await screen.findByText(/already exists/)).toBeInTheDocument()
    expect(screen.queryByText('detail page')).not.toBeInTheDocument()
  })
})

describe('FlagEditorPage — edit & version conflict', () => {
  test('rebases onto the current version after a 409', async () => {
    currentFlags = [makeFlag()] // v3
    renderEditor('/flags/checkout-cta/edit')

    const nameInput = await screen.findByDisplayValue('Checkout CTA experiment')
    await userEvent.clear(nameInput)
    await userEvent.type(nameInput, 'My new name')

    // Server moves to v4 behind our back before we submit.
    currentFlags = [makeFlag({ version: 4, name: 'Renamed elsewhere' })]

    await userEvent.click(screen.getByRole('button', { name: 'Review & save' }))
    await userEvent.click(await screen.findByRole('button', { name: 'Save changes' }))

    // First PUT carried the stale version and conflicted.
    await screen.findByText('Version conflict')
    expect(putBodies[0]).toMatchObject({ version: 3, name: 'My new name' })

    await userEvent.click(screen.getByRole('button', { name: 'Rebase my edits onto v4' }))

    // The review sheet reopens against v4; confirm submits successfully.
    await userEvent.click(await screen.findByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('detail page')).toBeInTheDocument()
    expect(putBodies[1]).toMatchObject({ version: 4, name: 'My new name' })
    await waitFor(() => expect(putBodies).toHaveLength(2))
  })

  test('an untouched edit form reports no changes instead of submitting', async () => {
    currentFlags = [makeFlag()]
    renderEditor('/flags/checkout-cta/edit')
    await screen.findByDisplayValue('Checkout CTA experiment')
    await userEvent.click(screen.getByRole('button', { name: 'Review & save' }))
    await waitFor(() => expect(screen.queryByText(/Update checkout-cta/)).not.toBeInTheDocument())
    expect(putBodies).toHaveLength(0)
  })
})
