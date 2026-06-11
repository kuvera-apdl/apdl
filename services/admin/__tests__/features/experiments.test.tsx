import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import {
  experimentEntrySchema,
  experimentResultSchema,
} from '../../src/api/schemas/experiments'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import {
  buildCreate,
  parseJsonArray,
  type ExperimentFormValues,
} from '../../src/features/experiments/ExperimentForm'
import { ExperimentListPage } from '../../src/features/experiments/ExperimentListPage'
import { seedWorkspace } from '../helpers/fixtures'

const EXPERIMENT = {
  key: 'checkout-test',
  status: 'running',
  description: 'CTA experiment',
  traffic_percentage: 100,
  variants: [{ key: 'control', weight: 1 }],
  start_date: '2026-06-01',
  end_date: '',
  created_at: '2026-06-01T00:00:00+00:00',
  updated_at: '2026-06-09T00:00:00+00:00',
}

const server = setupServer(
  http.get('http://localhost:8081/v1/admin/experiments', () =>
    HttpResponse.json({ experiments: [EXPERIMENT], count: 1 }),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
})

describe('experiment schemas', () => {
  test('list entries parse with and without the optional variants key', () => {
    expect(experimentEntrySchema.safeParse(EXPERIMENT).success).toBe(true)
    const { variants: _variants, ...withoutVariants } = EXPERIMENT
    expect(experimentEntrySchema.safeParse(withoutVariants).success).toBe(true)
  })

  test('experiment results parse the CI tuple and nullable stats', () => {
    expect(
      experimentResultSchema.safeParse({
        experiment_id: 'checkout-test',
        flag_key: 'checkout-cta',
        metric: 'purchase',
        method: 'frequentist',
        variants: [{ variant: 'control', users: 100, mean: 0.1, stddev: 0.3, total: 10 }],
        effect_size: 0.12,
        confidence_interval: [-0.01, 0.25],
        p_value: 0.04,
        is_significant: true,
        recommendation: 'Ship it',
      }).success,
    ).toBe(true)
    expect(
      experimentResultSchema.safeParse({
        experiment_id: 'x',
        flag_key: 'y',
        metric: 'z',
        method: 'bayesian',
        variants: [],
        effect_size: null,
        confidence_interval: null,
        p_value: null,
        is_significant: false,
        recommendation: 'Keep collecting data',
      }).success,
    ).toBe(true)
  })
})

describe('experiment form model', () => {
  test('parseJsonArray distinguishes empty (= unchanged) from invalid', () => {
    expect(parseJsonArray('')).toEqual({ value: null, error: null })
    expect(parseJsonArray('[1, 2]')).toEqual({ value: [1, 2], error: null })
    expect(parseJsonArray('{"a": 1}').error).toBe('Must be a JSON array')
    expect(parseJsonArray('{nope').error).toBe('Invalid JSON')
  })

  test('buildCreate trims and defaults the loose fields', () => {
    const values: ExperimentFormValues = {
      key: ' exp-1 ',
      status: '',
      description: 'd',
      traffic_percentage: 50,
      start_date: '2026-06-01',
      end_date: '',
      variantsJson: '[{"key":"control","weight":1}]',
      targetingRulesJson: '',
    }
    expect(buildCreate(values)).toEqual({
      key: 'exp-1',
      status: 'draft',
      description: 'd',
      traffic_percentage: 50,
      start_date: '2026-06-01',
      end_date: '',
      variants: [{ key: 'control', weight: 1 }],
      targeting_rules: [],
    })
  })
})

describe('ExperimentListPage', () => {
  test('renders experiments with status pills', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MemoryRouter initialEntries={['/experiments']}>
              <Routes>
                <Route path="/experiments" element={<ExperimentListPage />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )
    expect(await screen.findByText('checkout-test')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText('100%')).toBeInTheDocument()
    // Sanity: row click target exists.
    await userEvent.hover(screen.getByText('checkout-test'))
  })
})
