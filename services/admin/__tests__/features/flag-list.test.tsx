import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { FlagListPage } from '../../src/features/flags/FlagListPage'
import { makeFlag, seedWorkspace } from '../helpers/fixtures'

const server = setupServer(
  http.get('*/api/projects/demo/config/v1/admin/flags', () =>
    HttpResponse.json({
      flags: [
        makeFlag(),
        makeFlag({
          key: 'old-flag',
          name: 'Retired flag',
          state: 'archived',
          enabled: false,
          archived_at: '2026-06-01T00:00:00+00:00',
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

function renderFlagList() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter initialEntries={['/flags']}>
            <Routes>
              <Route path="/flags" element={<FlagListPage />} />
              <Route path="/flags/:key" element={<div>detail page</div>} />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('FlagListPage', () => {
  test('renders flags with state pills, hiding archived by default', async () => {
    renderFlagList()
    const keyCell = await screen.findByText('checkout-cta')
    const row = keyCell.closest('tr')
    expect(row).not.toBeNull()
    expect(within(row!).getByText('active')).toBeInTheDocument()
    expect(screen.queryByText('old-flag')).not.toBeInTheDocument()
    expect(screen.getByText('control/treatment 50:50')).toBeInTheDocument()
  })

  test('the archived toggle reveals archived flags', async () => {
    renderFlagList()
    await screen.findByText('checkout-cta')
    await userEvent.click(screen.getByRole('switch', { name: /show archived/i }))
    expect(await screen.findByText('old-flag')).toBeInTheDocument()
  })

  test('search filters by key/name/description', async () => {
    renderFlagList()
    await screen.findByText('checkout-cta')
    await userEvent.type(screen.getByPlaceholderText(/search key/i), 'does-not-exist')
    expect(await screen.findByText(/no flags match/i)).toBeInTheDocument()
  })

  test('clicking a row navigates to the detail route', async () => {
    renderFlagList()
    await userEvent.click(await screen.findByText('checkout-cta'))
    expect(await screen.findByText('detail page')).toBeInTheDocument()
  })
})
