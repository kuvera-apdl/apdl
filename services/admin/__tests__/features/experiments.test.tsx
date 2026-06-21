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
  emptyExperimentValues,
  parseTargetingRules,
  validateExperimentForm,
  type ExperimentFormValues,
} from '../../src/features/experiments/ExperimentForm'
import { ExperimentListPage } from '../../src/features/experiments/ExperimentListPage'
import { seedWorkspace } from '../helpers/fixtures'

const EXPERIMENT = {
  key: 'checkout-test',
  flag_key: 'checkout-test',
  status: 'running',
  description: 'CTA experiment',
  default_variant: 'control',
  traffic_percentage: 100,
  variants: [
    { key: 'control', weight: 1 },
    { key: 'treatment', weight: 1 },
  ],
  targeting_rules: [],
  primary_metric: null,
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
  test('list entries parse into the canonical record', () => {
    expect(experimentEntrySchema.safeParse(EXPERIMENT).success).toBe(true)
    // The record is canonical now — the flag link is required, not optional.
    const { flag_key: _flagKey, ...withoutFlagKey } = EXPERIMENT
    expect(experimentEntrySchema.safeParse(withoutFlagKey).success).toBe(false)
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
  test('parseTargetingRules validates against the canonical GateRule schema', () => {
    expect(parseTargetingRules('')).toEqual({ value: [], error: null })
    const rule = {
      id: 'r1',
      name: '',
      conditions: [],
      rollout: { percentage: 100, bucket_by: 'user_id' },
    }
    expect(parseTargetingRules(JSON.stringify([rule]))).toEqual({ value: [rule], error: null })
    expect(parseTargetingRules('{"a": 1}').error).toBe('Must be a JSON array of rules')
    expect(parseTargetingRules('[{"id":"r1"}]').error).toBe(
      'Each rule needs id, name, conditions, and a rollout',
    )
    expect(parseTargetingRules('{nope').error).toBe('Invalid JSON')
  })

  test('buildCreate projects the structured form to the canonical payload', () => {
    const values: ExperimentFormValues = {
      key: ' exp-1 ',
      flagKey: '',
      status: 'running',
      description: 'd',
      traffic_percentage: 50,
      start_date: '2026-06-01',
      end_date: '',
      variants: [
        { key: 'control', weight: 1, description: 'Current' },
        { key: 'treatment', weight: 2, description: '' },
      ],
      default_variant: 'control',
      metricEvent: 'purchase',
      metricType: 'conversion',
      metricDirection: 'increase',
      targetingRulesJson: '',
    }
    expect(buildCreate(values)).toEqual({
      key: 'exp-1',
      flag_key: 'exp-1',
      status: 'running',
      description: 'd',
      traffic_percentage: 50,
      start_date: '2026-06-01',
      end_date: '',
      variants: [
        { key: 'control', weight: 1, description: 'Current' },
        { key: 'treatment', weight: 2 },
      ],
      default_variant: 'control',
      primary_metric: { event: 'purchase', type: 'conversion', direction: 'increase' },
      targeting_rules: [],
    })
  })

  test('validateExperimentForm catches duplicate keys and an out-of-set default', () => {
    expect(validateExperimentForm(emptyExperimentValues())).toEqual({})

    const duplicate = {
      ...emptyExperimentValues(),
      variants: [
        { key: 'a', weight: 1, description: '' },
        { key: 'a', weight: 1, description: '' },
      ],
    }
    expect(validateExperimentForm(duplicate).variants).toBe('Variant keys must be unique')

    const badDefault = { ...emptyExperimentValues(), default_variant: 'nope' }
    expect(validateExperimentForm(badDefault).default_variant).toBeTruthy()
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
