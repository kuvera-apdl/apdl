import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { ExperimentResultsTab } from '../../src/features/experiments/ExperimentResultsTab'
import { seedWorkspace } from '../helpers/fixtures'

const COMMON_RESULT = {
  experiment_key: 'checkout-test',
  flag_key: 'checkout-experiment',
  experiment_status: 'running',
  control_variant: 'control',
  metric_event: 'purchase',
  start_date: '2026-06-01T00:00:00+00:00',
  end_date: '2026-06-15T00:00:00+00:00',
  config_version: 7,
  arms: [
    { variant: 'control', sample_size: 100, conversions: 10, conversion_rate: 0.1 },
    { variant: 'blue', sample_size: 100, conversions: 20, conversion_rate: 0.2 },
    { variant: 'green', sample_size: 100, conversions: 15, conversion_rate: 0.15 },
  ],
  crossover_actors: 4,
  unknown_variant_actors: 2,
}

const READY_RESULT = {
  analysis_status: 'ready',
  ...COMMON_RESULT,
  significance_level: 0.05,
  correction: 'bonferroni',
  comparisons: [
    {
      control_variant: 'control',
      treatment_variant: 'blue',
      control_rate: 0.1,
      treatment_rate: 0.2,
      rate_difference: 0.1,
      confidence_interval: [0.02, 0.18],
      raw_p_value: 0.001,
      adjusted_p_value: 0.002,
      is_significant: true,
    },
    {
      control_variant: 'control',
      treatment_variant: 'green',
      control_rate: 0.1,
      treatment_rate: 0.15,
      rate_difference: 0.05,
      confidence_interval: [-0.01, 0.11],
      raw_p_value: 0.04,
      adjusted_p_value: 0.08,
      is_significant: false,
    },
  ],
}

let requestedUrl: URL | null = null

const server = setupServer(
  http.get('*/api/projects/demo/query/v1/query/experiment/:key', ({ request }) => {
    requestedUrl = new URL(request.url)
    return HttpResponse.json(READY_RESULT)
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  requestedUrl = null
})

function renderResults() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <MemoryRouter>
            <ExperimentResultsTab experimentKey="checkout-test" />
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('ExperimentResultsTab', () => {
  test('uses only the canonical project-scoped endpoint and renders every comparison', async () => {
    renderResults()

    expect(await screen.findByText('Authoritative experiment analysis')).toBeInTheDocument()
    expect(requestedUrl?.pathname).toBe(
      '/api/projects/demo/query/v1/query/experiment/checkout-test',
    )
    expect(Array.from(requestedUrl?.searchParams.keys() ?? [])).toEqual(['project_id'])
    expect(requestedUrl?.searchParams.get('project_id')).toBe('demo')

    expect(screen.getByText('checkout-experiment')).toBeInTheDocument()
    expect(screen.getByText('purchase')).toBeInTheDocument()
    expect(screen.getByText('v7')).toBeInTheDocument()
    expect(screen.getByText('blue vs control')).toBeInTheDocument()
    expect(screen.getByText('green vs control')).toBeInTheDocument()
    expect(screen.getByText('+10.00 pp')).toBeInTheDocument()
    expect(screen.getByText('+5.00 pp')).toBeInTheDocument()
    expect(screen.getByText(/do not trigger or recommend ship\/rollback actions/i)).toBeInTheDocument()

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /compute/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Ship it')).not.toBeInTheDocument()
  })

  test('renders typed insufficient data without a comparison or recommendation', async () => {
    server.use(
      http.get('*/api/projects/demo/query/v1/query/experiment/:key', () =>
        HttpResponse.json({
          analysis_status: 'insufficient_data',
          ...COMMON_RESULT,
          arms: [
            { variant: 'control', sample_size: 20, conversions: 5, conversion_rate: 0.25 },
            { variant: 'blue', sample_size: 1, conversions: 1, conversion_rate: 1 },
            { variant: 'green', sample_size: 20, conversions: 4, conversion_rate: 0.2 },
          ],
          reason: 'underpowered_arms',
          minimum_sample_size_per_arm: 2,
          underpowered_variants: ['blue'],
        }),
      ),
    )

    renderResults()

    expect(
      await screen.findByText('Insufficient data — One or more arms need more traffic'),
    ).toBeInTheDocument()
    expect(screen.getByText(/minimum sample size per arm/i)).toHaveTextContent('2')
    expect(screen.getByText(/underpowered variants/i)).toHaveTextContent('blue')
    expect(screen.queryByText('All treatment comparisons')).not.toBeInTheDocument()
    expect(screen.queryByText('Keep collecting data')).not.toBeInTheDocument()
  })
})
