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
  experiment_status: 'completed',
  control_variant: 'control',
  metric_event: 'purchase',
  metric_direction: 'increase',
  statistical_plan: {
    protocol: 'fixed_horizon_fisher_newcombe_cc_plan_v1',
    baseline_conversion_rate: 0.1,
    minimum_detectable_effect: 0.02,
    significance_level: 0.05,
    nominal_power: 0.8,
    required_sample_size_per_arm: 100,
    data_settlement_seconds: 300,
  },
  start_date: '2026-06-01T00:00:00+00:00',
  end_date: '2026-06-15T00:00:00+00:00',
  config_version: 7,
  arms: [
    { variant: 'control', sample_size: 100, conversions: 10, conversion_rate: 0.1 },
    { variant: 'blue', sample_size: 100, conversions: 20, conversion_rate: 0.2 },
    { variant: 'green', sample_size: 100, conversions: 15, conversion_rate: 0.15 },
  ],
  crossover_actors: 4,
  unknown_variant_actors: 0,
  identity_conflict_actors: 0,
  identity_quality: 'unambiguous',
  data_completeness: 'not_verified',
  deployment_readiness: 'not_assessed',
}

const FINAL_RESULT = {
  analysis_status: 'decision_snapshot',
  ...COMMON_RESULT,
  inference_method: 'fisher_exact_two_sided',
  interval_method: 'newcombe_wilson',
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
      is_statistically_significant: true,
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
      is_statistically_significant: false,
    },
  ],
}

let requestedUrl: URL | null = null

const server = setupServer(
  http.get('*/api/projects/demo/query/v1/query/experiment/:key', ({ request }) => {
    requestedUrl = new URL(request.url)
    return HttpResponse.json(FINAL_RESULT)
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
    expect(screen.getByText('+10.00 pp')).toHaveClass('text-emerald-600')
    expect(screen.getByText('+5.00 pp')).toBeInTheDocument()
    expect(screen.getByText(/do not trigger or recommend ship\/rollback actions/i)).toBeInTheDocument()

    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /compute/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Ship it')).not.toBeInTheDocument()
  })

  test('colors effects using the declared metric direction without asserting readiness', async () => {
    server.use(
      http.get('*/api/projects/demo/query/v1/query/experiment/:key', () =>
        HttpResponse.json({
          ...FINAL_RESULT,
          metric_direction: 'decrease',
          comparisons: [
            {
              ...FINAL_RESULT.comparisons[0],
              treatment_rate: 0,
              rate_difference: -0.1,
              confidence_interval: [-0.18, -0.02],
            },
          ],
        }),
      ),
    )

    renderResults()

    expect(await screen.findByText('-10.00 pp')).toHaveClass('text-emerald-600')
    expect(screen.getByText('not_assessed')).toBeInTheDocument()
  })

  test('renders typed non-final data without a comparison or recommendation', async () => {
    server.use(
      http.get('*/api/projects/demo/query/v1/query/experiment/:key', () =>
        HttpResponse.json({
          analysis_status: 'non_final',
          ...COMMON_RESULT,
          arms: [
            { variant: 'control', sample_size: 20, conversions: 5, conversion_rate: 0.25 },
            { variant: 'blue', sample_size: 1, conversions: 1, conversion_rate: 1 },
            { variant: 'green', sample_size: 20, conversions: 4, conversion_rate: 0.2 },
          ],
          reason: 'underpowered_arms',
          underpowered_variants: ['blue'],
        }),
      ),
    )

    renderResults()

    expect(
      await screen.findByText('Non-final analysis — One or more arms need more traffic'),
    ).toBeInTheDocument()
    expect(screen.getByText(/predeclared sample target per arm/i)).toHaveTextContent('100')
    expect(screen.getByText(/underpowered variants/i)).toHaveTextContent('blue')
    expect(screen.queryByText('All treatment comparisons')).not.toBeInTheDocument()
    expect(screen.queryByText('Keep collecting data')).not.toBeInTheDocument()
  })

  test('renders unknown variant exposures as an explicit non-final state', async () => {
    server.use(
      http.get('*/api/projects/demo/query/v1/query/experiment/:key', () =>
        HttpResponse.json({
          analysis_status: 'non_final',
          ...COMMON_RESULT,
          unknown_variant_actors: 2,
          reason: 'unknown_variant_exposures',
          underpowered_variants: [],
        }),
      ),
    )

    renderResults()

    expect(
      await screen.findByText(
        'Non-final analysis — Unknown experiment variants were exposed',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/unknown-variant actors/i).parentElement).toHaveTextContent('2')
    expect(screen.queryByText('All treatment comparisons')).not.toBeInTheDocument()
  })
})
