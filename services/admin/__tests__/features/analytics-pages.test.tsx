import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { EventsExplorerPage } from '../../src/features/analytics/EventsExplorerPage'
import { FunnelsPage } from '../../src/features/analytics/FunnelsPage'
import { seedWorkspace } from '../helpers/fixtures'

const requests: { path: string; body: unknown }[] = []

const server = setupServer(
  http.post('http://localhost:8082/v1/query/events/count', async ({ request }) => {
    requests.push({ path: 'count', body: await request.json() })
    return HttpResponse.json({
      results: [
        { selector: 'page', event_name: 'page', event_count: 120, unique_users: 48 },
      ],
      total_events: 120,
      total_users: 48,
    })
  }),
  http.post('http://localhost:8082/v1/query/funnel', async ({ request }) => {
    requests.push({ path: 'funnel', body: await request.json() })
    return HttpResponse.json({
      steps: [
        {
          step: 1,
          event_name: 'page',
          selector: 'page',
          count: 100,
          conversion_rate: 100,
          overall_rate: 100,
        },
        {
          step: 2,
          event_name: '$click',
          selector: '$click',
          count: 25,
          conversion_rate: 25,
          overall_rate: 25,
        },
      ],
      overall_conversion: 25,
    })
  }),
  http.post('http://localhost:8082/v1/query/events/names', async () => {
    return HttpResponse.json({
      events: [
        { event_name: 'page', event_count: 76, unique_users: 11 },
        { event_name: '$click', event_count: 162, unique_users: 7 },
        { event_name: '$web_vital', event_count: 49, unique_users: 5 },
      ],
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

function renderPage(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('EventsExplorerPage', () => {
  test('runs a counts query with project_id and renders the result table', async () => {
    renderPage(<EventsExplorerPage />)
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(await screen.findByText('120')).toBeInTheDocument()
    expect(screen.getByText(/48 users/)).toBeInTheDocument()
    expect(requests[0]?.body).toMatchObject({
      project_id: 'demo',
      selectors: [{ event_name: 'page', filters: [] }],
    })
  })

  test('refuses to run with an invalid selector', async () => {
    renderPage(<EventsExplorerPage />)
    await userEvent.click(screen.getByRole('button', { name: 'Clear Selector 1 event name' }))
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))
    expect(requests).toHaveLength(0)
  })
})

describe('FunnelsPage', () => {
  test('runs a funnel and highlights the drop-off', async () => {
    renderPage(<FunnelsPage />)
    await userEvent.click(screen.getByRole('button', { name: 'Run funnel' }))

    expect(await screen.findByText('25%')).toBeInTheDocument()
    expect(screen.getByText(/−75% between step 1 and 2/)).toBeInTheDocument()
    expect(screen.getByText(/biggest drop-off/)).toBeInTheDocument()
    expect(requests[0]?.body).toMatchObject({ window_days: 7, project_id: 'demo' })
  })
})
