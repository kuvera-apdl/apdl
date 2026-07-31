import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { EventsExplorerPage } from '../../src/features/analytics/EventsExplorerPage'
import { FunnelsPage } from '../../src/features/analytics/FunnelsPage'
import { CohortsPage } from '../../src/features/analytics/CohortsPage'
import { RetentionPage } from '../../src/features/analytics/RetentionPage'
import { seedWorkspace } from '../helpers/fixtures'

const requests: { path: string; body: unknown }[] = []
const catalogRequests: unknown[] = []

const server = setupServer(
  http.post('*/api/projects/demo/query/v1/query/events/count', async ({ request }) => {
    requests.push({ path: 'count', body: await request.json() })
    return HttpResponse.json({
      results: [
        { selector: 'page', event_name: 'page', event_count: 120, unique_users: 48 },
      ],
      total_events: 120,
      total_users: 48,
    })
  }),
  http.post('*/api/projects/demo/query/v1/query/events/breakdown', async ({ request }) => {
    requests.push({ path: 'breakdown', body: await request.json() })
    return HttpResponse.json({
      selector: '$click',
      property: 'score',
      results: [
        {
          selector: '$click',
          property_type: 'integer',
          property_value: '1',
          event_count: 9,
          unique_users: 7,
        },
        {
          selector: '$click',
          property_type: 'float',
          property_value: '1',
          event_count: 4,
          unique_users: 3,
        },
      ],
    })
  }),
  http.post('*/api/projects/demo/query/v1/query/funnel', async ({ request }) => {
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
  http.post('*/api/projects/demo/query/v1/query/retention', async ({ request }) => {
    requests.push({ path: 'retention', body: await request.json() })
    return HttpResponse.json({
      cohort_mode: 'first_match_in_window',
      cohort_selector: 'page',
      return_selector: 'page',
      cohorts: [],
    })
  }),
  http.post('*/api/projects/demo/query/v1/query/events/names', async ({ request }) => {
    catalogRequests.push(await request.json())
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
  catalogRequests.length = 0
})

function renderPage(ui: React.ReactElement) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter>{ui}</MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('EventsExplorerPage', () => {
  test('loads the event catalog through the current UTC date', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-07-30T03:00:00Z'))
    try {
      renderPage(<EventsExplorerPage />)

      await waitFor(() => expect(catalogRequests).toHaveLength(1))
      expect(catalogRequests[0]).toEqual({
        project_id: 'demo',
        start_date: '2026-05-02',
        end_date: '2026-07-30',
        limit: 1000,
      })
    } finally {
      vi.useRealTimers()
    }
  })

  test.each([
    ['Events Explorer', <EventsExplorerPage />, '2026-07-24'],
    ['Funnels', <FunnelsPage />, '2026-07-24'],
    ['Cohorts', <CohortsPage />, '2026-07-01'],
    ['Retention', <RetentionPage />, '2026-07-01'],
  ])('%s defaults to an inclusive UTC range', (_name, page, startDate) => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-07-30T03:00:00Z'))
    try {
      const view = renderPage(page)
      expect(screen.getByLabelText('Start date (UTC)')).toHaveValue(startDate)
      expect(screen.getByLabelText('End date (UTC)')).toHaveValue('2026-07-30')
      expect(screen.getByText('UTC')).toBeVisible()
      view.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

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

  test('renders scalar types that distinguish equal canonical values', async () => {
    renderPage(<EventsExplorerPage />)
    await userEvent.click(screen.getByRole('tab', { name: 'Breakdown' }))
    await userEvent.type(screen.getByPlaceholderText('href'), 'score')
    await userEvent.click(screen.getByRole('button', { name: 'Run' }))

    expect(await screen.findByText('integer')).toBeInTheDocument()
    expect(screen.getByText('float')).toBeInTheDocument()
    expect(requests[0]?.body).toMatchObject({
      project_id: 'demo',
      property: 'score',
    })
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

describe('RetentionPage', () => {
  test('declares and explains first-match-in-window cohorts', async () => {
    renderPage(<RetentionPage />)

    expect(screen.getByRole('heading', { name: 'Window-relative retention' })).toBeInTheDocument()
    expect(screen.getByText(/Existing actors may re-enter/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Run retention' }))

    expect(await screen.findByText('No cohorts in range')).toBeInTheDocument()
    expect(requests[0]?.body).toMatchObject({
      project_id: 'demo',
      cohort_mode: 'first_match_in_window',
    })
  })
})
