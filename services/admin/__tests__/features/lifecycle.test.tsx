import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { isCleanupCandidate, LifecycleDialog } from '../../src/features/flags/LifecycleDialog'
import { makeFlag, seedWorkspace } from '../helpers/fixtures'

const requests: { method: string; path: string; body: unknown }[] = []

const server = setupServer(
  http.put('http://localhost:8081/v1/admin/flags/:key', async ({ request, params }) => {
    requests.push({
      method: 'PUT',
      path: String(params.key),
      body: await request.json(),
    })
    return HttpResponse.json({ updated: true, flag: makeFlag({ version: 4 }) })
  }),
  http.post('http://localhost:8081/v1/admin/flags/:key/disable', async ({ request, params }) => {
    requests.push({
      method: 'POST',
      path: `${String(params.key)}/disable`,
      body: await request.json(),
    })
    return HttpResponse.json({
      disabled: true,
      flag: makeFlag({ state: 'disabled', enabled: false, disabled_reason: 'guardrail_failed' }),
    })
  }),
  http.delete('http://localhost:8081/v1/admin/flags/:key', ({ params }) => {
    requests.push({
      method: 'DELETE',
      path: String(params.key),
      body: null,
    })
    return HttpResponse.json({
      archived: true,
      flag: makeFlag({ state: 'archived', enabled: false, archived_at: '2026-06-10T00:00:00+00:00' }),
    })
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  requests.length = 0
})

function renderDialog(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>{ui}</TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('LifecycleDialog', () => {
  test('activate sends PUT {state: active} with the optimistic-lock version', async () => {
    const flag = makeFlag({ state: 'draft', enabled: false })
    const onClose = vi.fn()
    renderDialog(<LifecycleDialog flag={flag} action="activate" onClose={onClose} />)

    await userEvent.click(await screen.findByRole('button', { name: 'Activate' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(requests[0]).toMatchObject({
      method: 'PUT',
      path: 'checkout-cta',
      body: { version: 3, state: 'active' },
    })
  })

  test('disable requires a reason and records the evidence note', async () => {
    const flag = makeFlag()
    const onClose = vi.fn()
    renderDialog(<LifecycleDialog flag={flag} action="disable" onClose={onClose} />)

    await userEvent.type(
      await screen.findByPlaceholderText('What prompted this kill switch?'),
      'errors spiking',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Disable flag' }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(requests[0]).toMatchObject({
      method: 'POST',
      path: 'checkout-cta/disable',
      body: { reason: 'guardrail_failed', source: 'admin', evidence: { note: 'errors spiking' } },
    })
  })

  test('archive is gated behind typing the flag key', async () => {
    const flag = makeFlag()
    const onClose = vi.fn()
    renderDialog(<LifecycleDialog flag={flag} action="archive" onClose={onClose} />)

    const confirmButton = await screen.findByRole('button', { name: 'Archive forever' })
    expect(confirmButton).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Confirm flag key'), 'checkout-cta')
    expect(confirmButton).toBeEnabled()
    await userEvent.click(confirmButton)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(requests[0]).toMatchObject({ method: 'DELETE', path: 'checkout-cta' })
  })
})

describe('isCleanupCandidate', () => {
  test('mirrors the server eligibility rules', () => {
    expect(isCleanupCandidate(makeFlag())).toBe(false) // has rules, 10% fallthrough
    const eligible = makeFlag({
      rules: [],
      fallthrough: { rollout: { percentage: 100, bucket_by: 'user_id' } },
      variants: [
        { key: 'control', weight: 0 },
        { key: 'treatment', weight: 1 },
      ],
      default_variant: 'control',
    })
    expect(isCleanupCandidate(eligible)).toBe(true)
    // The single winning variant must differ from the default.
    expect(
      isCleanupCandidate({ ...eligible, default_variant: 'treatment' }),
    ).toBe(false)
  })
})
