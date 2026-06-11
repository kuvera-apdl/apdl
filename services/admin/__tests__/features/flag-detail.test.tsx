import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { FlagDetailPage } from '../../src/features/flags/FlagDetailPage'
import { makeAuditEntry, makeFlag, seedWorkspace } from '../helpers/fixtures'

const server = setupServer(
  http.get('http://localhost:8081/v1/admin/flags', () =>
    HttpResponse.json({ flags: [makeFlag()], count: 1 }),
  ),
  http.get('http://localhost:8081/v1/admin/flags/:key/audit', () =>
    HttpResponse.json({
      flag_key: 'checkout-cta',
      audit: [
        makeAuditEntry(),
        makeAuditEntry({
          id: 41,
          action: 'flag_created',
          actor: 'kirill',
          previous_version: null,
          new_version: 1,
          before: null,
          after: { key: 'checkout-cta', version: 1 },
        }),
      ],
      count: 2,
    }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
})

function renderDetail(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/flags/:key" element={<FlagDetailPage />} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('FlagDetailPage', () => {
  test('renders the header with state, version, and evaluation mode', async () => {
    renderDetail('/flags/checkout-cta')
    expect(await screen.findByText('checkout-cta')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('v3')).toBeInTheDocument()
    // evaluation_mode appears in the header badge and the overview tab.
    expect(screen.getAllByText('client').length).toBeGreaterThan(0)
  })

  test('targeting tab renders rules in evaluation order with fallthrough', async () => {
    renderDetail('/flags/checkout-cta')
    await screen.findByText('checkout-cta')
    await userEvent.click(screen.getByRole('tab', { name: 'Targeting' }))
    expect(await screen.findByText('beta users')).toBeInTheDocument()
    expect(screen.getByText('plan')).toBeInTheDocument()
    expect(screen.getByText('Fallthrough')).toBeInTheDocument()
  })

  test('audit tab renders the timeline with version transitions', async () => {
    renderDetail('/flags/checkout-cta?tab=audit')
    expect(await screen.findByText('v2 → v3')).toBeInTheDocument()
    expect(screen.getAllByText('kirill').length).toBeGreaterThan(0)
    expect(screen.getByText('created')).toBeInTheDocument()
  })

  test('unknown keys render the not-found state', async () => {
    renderDetail('/flags/nope')
    expect(await screen.findByText(/not found/i)).toBeInTheDocument()
  })
})
