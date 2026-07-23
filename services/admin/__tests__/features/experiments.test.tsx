import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { createMemoryRouter, MemoryRouter, Route, RouterProvider, Routes } from 'react-router-dom'
import { toast } from 'sonner'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test, vi } from 'vitest'

import { deleteExperiment } from '../../src/api/experiments'
import {
  experimentCreateResponseSchema,
  experimentCreateSchema,
  experimentDeleteResponseSchema,
  experimentEntrySchema,
  experimentResultSchema,
  experimentUpdateResponseSchema,
  experimentUpdateSchema,
} from '../../src/api/schemas/experiments'
import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import type { Workspace } from '../../src/core/workspace'
import {
  buildCreate,
  buildUpdate,
  emptyExperimentValues,
  entryToFormValues,
  parseTargetingRules,
  validateExperimentForm,
  type ExperimentFormValues,
} from '../../src/features/experiments/ExperimentForm'
import { ExperimentListPage } from '../../src/features/experiments/ExperimentListPage'
import { ExperimentDetailPage } from '../../src/features/experiments/ExperimentDetailPage'
import { makeWorkspace, seedWorkspace } from '../helpers/fixtures'

const STATISTICAL_PLAN = {
  protocol: 'fixed_horizon_fisher_newcombe_cc_plan_v1',
  baseline_conversion_rate: 0.5,
  minimum_detectable_effect: 0.5,
  significance_level: 0.05,
  nominal_power: 0.8,
  required_sample_size_per_arm: 20,
  data_settlement_seconds: 300,
} as const

const EXPERIMENT = {
  key: 'checkout-test',
  flag_key: 'checkout-test',
  bucket_by: 'anonymous_id',
  status: 'running',
  description: 'CTA experiment',
  default_variant: 'control',
  traffic_percentage: 100,
  variants: [
    { key: 'control', weight: 1 },
    { key: 'treatment', weight: 1 },
  ],
  targeting_rules: [],
  primary_metric: { event: 'purchase', type: 'conversion', direction: 'increase' },
  statistical_plan: STATISTICAL_PLAN,
  start_date: '2026-06-01T00:00:00+00:00',
  end_date: '2026-07-01T00:00:00+00:00',
  version: 2,
  created_at: '2026-06-01T00:00:00+00:00',
  updated_at: '2026-06-09T00:00:00+00:00',
  archived_at: null,
  archived_by: null,
}

const ARCHIVED_EXPERIMENT = {
  ...EXPERIMENT,
  status: 'stopped',
  version: 3,
  archived_at: '2026-06-10T00:00:00+00:00',
  archived_by: 'credential:operator',
}

let deleteRequestUrl = ''
const updateBodies: Record<string, unknown>[] = []
const updateKeys: string[] = []

const server = setupServer(
  http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
    HttpResponse.json({ experiments: [EXPERIMENT], count: 1 }),
  ),
  http.delete('http://config.test/v1/admin/experiments/:key', ({ request, params }) => {
    deleteRequestUrl = request.url
    return HttpResponse.json({
      deleted: false,
      archived: true,
      key: String(params.key),
      flag_key: 'checkout-test',
      version: 3,
    })
  }),
  http.put('*/api/projects/demo/config/v1/admin/experiments/:key', async ({ request, params }) => {
    updateBodies.push((await request.json()) as Record<string, unknown>)
    updateKeys.push(String(params.key))
    return HttpResponse.json({
      updated: true,
      key: String(params.key),
      flag_key: 'checkout-test',
      bucket_by: 'anonymous_id',
      version: 3,
    })
  }),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  vi.restoreAllMocks()
})
afterAll(() => server.close())

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
  deleteRequestUrl = ''
  updateBodies.length = 0
  updateKeys.length = 0
})

describe('experiment schemas', () => {
  test('list entries parse into the canonical record', () => {
    expect(experimentEntrySchema.safeParse(EXPERIMENT).success).toBe(true)
    // The record is canonical now — the flag link is required, not optional.
    const { flag_key: _flagKey, ...withoutFlagKey } = EXPERIMENT
    expect(experimentEntrySchema.safeParse(withoutFlagKey).success).toBe(false)
    const { bucket_by: _bucketBy, ...withoutBucketBy } = EXPERIMENT
    expect(experimentEntrySchema.safeParse(withoutBucketBy).success).toBe(false)
    expect(experimentEntrySchema.safeParse({ ...EXPERIMENT, bucket_by: 'account_id' }).success)
      .toBe(false)
    expect(experimentEntrySchema.safeParse({ ...EXPERIMENT, status: 'scheduled' }).success).toBe(true)
    expect(experimentEntrySchema.safeParse({ ...EXPERIMENT, start_date: '2026-06-01' }).success).toBe(false)
    expect(experimentEntrySchema.safeParse(ARCHIVED_EXPERIMENT).success).toBe(true)
    const { archived_by: _archivedBy, ...withoutArchivedBy } = ARCHIVED_EXPERIMENT
    expect(experimentEntrySchema.safeParse(withoutArchivedBy).success).toBe(false)
  })

  test('write schemas require versions and response versions', () => {
    const create = buildCreate({ ...emptyExperimentValues(), key: 'checkout-test' })
    expect(experimentCreateSchema.safeParse(create).success).toBe(true)
    const { bucket_by: _bucketBy, ...createWithoutBucketBy } = create
    expect(experimentCreateSchema.safeParse(createWithoutBucketBy).success).toBe(false)
    expect(experimentCreateSchema.safeParse({ ...create, bucket_by: 'account_id' }).success)
      .toBe(false)
    expect(experimentCreateSchema.safeParse({ ...create, status: 'completed' }).success).toBe(false)
    expect(experimentCreateSchema.safeParse({ ...create, status: 'stopped' }).success).toBe(false)
    expect(experimentUpdateSchema.safeParse({ version: 2, description: 'updated' }).success).toBe(true)
    expect(experimentUpdateSchema.safeParse({ version: 2, bucket_by: 'user_id' }).success).toBe(true)
    expect(experimentUpdateSchema.safeParse({ version: 2, bucket_by: null }).success).toBe(false)
    expect(experimentUpdateSchema.safeParse({ description: 'updated' }).success).toBe(false)
    expect(
      experimentCreateResponseSchema.safeParse({
        created: true,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        bucket_by: 'anonymous_id',
        version: 1,
      }).success,
    ).toBe(true)
    expect(
      experimentUpdateResponseSchema.safeParse({
        updated: true,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        version: 3,
      }).success,
    ).toBe(false)
    expect(
      experimentCreateResponseSchema.safeParse({
        created: false,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        bucket_by: 'user_id',
        version: 1,
      }).success,
    ).toBe(true)
    expect(
      experimentCreateResponseSchema.safeParse({
        created: false,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        version: 1,
      }).success,
    ).toBe(false)
    expect(
      experimentUpdateResponseSchema.safeParse({
        updated: true,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        bucket_by: 'anonymous_id',
        version: 3,
      }).success,
    ).toBe(true)
    expect(
      experimentDeleteResponseSchema.safeParse({
        deleted: false,
        archived: true,
        key: 'checkout-test',
        flag_key: 'checkout-flag',
        version: 4,
      }).success,
    ).toBe(true)
  })

  test('write schemas mirror strict experiment variant and metric constraints', () => {
    const valid = buildCreate({ ...emptyExperimentValues(), key: 'checkout' })
    expect(experimentCreateSchema.safeParse(valid).success).toBe(true)

    const { default_variant: _defaultVariant, ...missingDefault } = valid
    expect(experimentCreateSchema.safeParse(missingDefault).success).toBe(false)
    expect(
      experimentCreateSchema.safeParse({
        ...valid,
        variants: [{ key: 'only', weight: 1 }],
        default_variant: 'only',
      }).success,
    ).toBe(false)
    expect(
      experimentCreateSchema.safeParse({
        ...valid,
        variants: Array.from({ length: 11 }, (_, index) => ({
          key: `variant-${index}`,
          weight: 1,
        })),
        default_variant: 'variant-0',
      }).success,
    ).toBe(false)
    expect(
      experimentCreateSchema.safeParse({
        ...valid,
        variants: [
          { key: 'control', weight: 0 },
          { key: 'treatment', weight: 1 },
        ],
      }).success,
    ).toBe(false)
    expect(
      experimentCreateSchema.safeParse({
        ...valid,
        primary_metric: { event: 'revenue', type: 'revenue', direction: 'increase' },
      }).success,
    ).toBe(false)
    expect(
      experimentUpdateSchema.safeParse({
        version: 1,
        variants: [{ key: 'only', weight: 1 }],
      }).success,
    ).toBe(false)
    expect(experimentCreateSchema.safeParse({ ...valid, key: 'bad/key' }).success).toBe(false)
    expect(
      experimentCreateSchema.safeParse({ ...valid, flag_key: 'bad key' }).success,
    ).toBe(false)
  })

  test('delete sends the optimistic version as a query parameter', async () => {
    await expect(
      deleteExperiment({ baseUrl: 'http://config.test', actor: 'tester' }, 'checkout-test', 2),
    ).resolves.toMatchObject({ deleted: false, archived: true, version: 3 })
    expect(new URL(deleteRequestUrl).searchParams.get('version')).toBe('2')
  })

  test('experiment results discriminate decision snapshots and finite non-final responses', () => {
    const common = {
      experiment_key: 'checkout-test',
      flag_key: 'checkout-cta',
      experiment_status: 'completed',
      control_variant: 'control',
      metric_event: 'purchase',
      metric_direction: 'increase',
      statistical_plan: STATISTICAL_PLAN,
      start_date: '2026-06-01T00:00:00+00:00',
      end_date: '2026-06-15T00:00:00+00:00',
      config_version: 3,
      arms: [
        { variant: 'control', sample_size: 100, conversions: 10, conversion_rate: 0.1 },
        { variant: 'treatment', sample_size: 100, conversions: 20, conversion_rate: 0.2 },
      ],
      crossover_actors: 1,
      unknown_variant_actors: 0,
      identity_conflict_actors: 0,
      identity_quality: 'unambiguous',
      data_completeness: 'not_verified',
      deployment_readiness: 'not_assessed',
    }
    const snapshot = {
      analysis_status: 'decision_snapshot',
      ...common,
      inference_method: 'fisher_exact_two_sided',
      interval_method: 'newcombe_wilson',
      correction: 'bonferroni',
      comparisons: [
        {
          control_variant: 'control',
          treatment_variant: 'treatment',
          control_rate: 0.1,
          treatment_rate: 0.2,
          rate_difference: 0.1,
          confidence_interval: [0.02, 0.18],
          raw_p_value: 0.01,
          adjusted_p_value: 0.01,
          is_statistically_significant: true,
        },
      ],
    }
    const nonFinal = {
      analysis_status: 'non_final',
      ...common,
      reason: 'underpowered_arms',
      underpowered_variants: ['treatment'],
    }

    expect(experimentResultSchema.safeParse(snapshot).success).toBe(true)
    expect(
      experimentResultSchema.safeParse({ ...snapshot, unknown_variant_actors: 1 }).success,
    ).toBe(false)
    expect(experimentResultSchema.safeParse(nonFinal).success).toBe(true)
    expect(
      experimentResultSchema.safeParse({
        ...nonFinal,
        unknown_variant_actors: 2,
        reason: 'unknown_variant_exposures',
        underpowered_variants: [],
      }).success,
    ).toBe(true)
    expect(
      experimentResultSchema.safeParse({
        ...snapshot,
        comparisons: [{ ...snapshot.comparisons[0], raw_p_value: Number.POSITIVE_INFINITY }],
      }).success,
    ).toBe(false)
    expect(
      experimentResultSchema.safeParse({ ...nonFinal, reason: 'not_enough_data' }).success,
    ).toBe(false)
    expect(
      experimentResultSchema.safeParse({ ...snapshot, recommendation: 'Ship it' }).success,
    ).toBe(false)
    expect(
      experimentResultSchema.safeParse({
        experiment_id: 'checkout-test',
        flag_key: 'checkout-cta',
        metric: 'purchase',
        method: 'frequentist',
        variants: [],
        recommendation: 'Ship it',
      }).success,
    ).toBe(false)
  })
})

describe('experiment form model', () => {
  test('parseTargetingRules validates eligibility without a competing rollout', () => {
    expect(parseTargetingRules('')).toEqual({ value: [], error: null })
    const rule = {
      id: 'r1',
      name: '',
      conditions: [],
    }
    expect(parseTargetingRules(JSON.stringify([rule]))).toEqual({ value: [rule], error: null })
    expect(parseTargetingRules(JSON.stringify([{
      ...rule,
      rollout: { percentage: 100, bucket_by: 'user_id' },
    }])).error).toBe('Each rule needs exactly id, name, and conditions')
    expect(parseTargetingRules('{"a": 1}').error).toBe('Must be a JSON array of rules')
    expect(parseTargetingRules('[{"id":"r1"}]').error).toBe(
      'Each rule needs exactly id, name, and conditions',
    )
    expect(parseTargetingRules('{nope').error).toBe('Invalid JSON')
  })

  test('buildCreate projects the structured form to the canonical payload', () => {
    const values: ExperimentFormValues = {
      key: ' exp-1 ',
      flagKey: '',
      status: 'running',
      description: 'd',
      bucket_by: 'anonymous_id',
      traffic_percentage: 50,
      start_date: '2026-06-01',
      end_date: '',
      variants: [
        { key: 'control', weight: 1, description: 'Current' },
        { key: 'treatment', weight: 2, description: '' },
      ],
      default_variant: 'control',
      metricEvent: 'purchase',
      metricDirection: 'increase',
      baselineConversionRate: 0.5,
      minimumDetectableEffect: 0.5,
      significanceLevel: 0.05,
      nominalPower: 0.8,
      requiredSampleSizePerArm: 20,
      dataSettlementSeconds: 300,
      targetingRulesJson: '',
    }
    expect(buildCreate(values)).toEqual({
      key: 'exp-1',
      flag_key: 'exp-1',
      status: 'running',
      description: 'd',
      bucket_by: 'anonymous_id',
      traffic_percentage: 50,
      start_date: '2026-06-01T00:00:00Z',
      end_date: null,
      variants: [
        { key: 'control', weight: 1, description: 'Current' },
        { key: 'treatment', weight: 2 },
      ],
      default_variant: 'control',
      primary_metric: { event: 'purchase', type: 'conversion', direction: 'increase' },
      statistical_plan: STATISTICAL_PLAN,
      targeting_rules: [],
    })

  })

  test('buildUpdate diffs drafts and never sends frozen fields after draft', () => {
    const draft = experimentEntrySchema.parse({
      ...EXPERIMENT,
      status: 'draft',
      primary_metric: null,
      statistical_plan: null,
      start_date: null,
      end_date: null,
    })
    const draftValues = entryToFormValues(draft)
    draftValues.description = 'Changed'
    draftValues.bucket_by = 'user_id'
    draftValues.start_date = '2026-06-01'
    draftValues.traffic_percentage = 50
    draftValues.targetingRulesJson = JSON.stringify([
      {
        id: 'rule-pro',
        name: 'Pro users',
        conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }],
      },
    ])
    expect(buildUpdate(draftValues, draft, 7)).toEqual({
      version: 7,
      description: 'Changed',
      bucket_by: 'user_id',
      traffic_percentage: 50,
      targeting_rules: [
        {
          id: 'rule-pro',
          name: 'Pro users',
          conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }],
        },
      ],
      start_date: '2026-06-01T00:00:00Z',
    })

    const running = experimentEntrySchema.parse({
      ...EXPERIMENT,
      primary_metric: { event: 'purchase', type: 'conversion', direction: 'increase' },
      end_date: '2026-07-01T00:00:00+00:00',
    })
    const stoppedValues = entryToFormValues(running)
    stoppedValues.status = 'stopped'
    stoppedValues.start_date = '2026-06-02'
    stoppedValues.end_date = '2026-07-02'
    stoppedValues.variants[1]!.weight = 2
    stoppedValues.default_variant = 'treatment'
    stoppedValues.metricEvent = 'checkout_completed'
    stoppedValues.traffic_percentage = 25
    stoppedValues.targetingRulesJson = JSON.stringify([
      {
        id: 'rule-pro',
        name: 'Pro users',
        conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }],
      },
    ])

    expect(buildUpdate(stoppedValues, running)).toEqual({ version: 2, status: 'stopped' })
  })

  test('validateExperimentForm catches duplicate keys and an out-of-set default', () => {
    const valid = { ...emptyExperimentValues(), key: 'experiment-1' }
    expect(validateExperimentForm(valid)).toEqual({})

    const duplicate = {
      ...valid,
      variants: [
        { key: 'a', weight: 1, description: '' },
        { key: 'a', weight: 1, description: '' },
      ],
    }
    expect(validateExperimentForm(duplicate).variants).toBe('Variant keys must be unique')

    const badDefault = { ...valid, default_variant: 'nope' }
    expect(validateExperimentForm(badDefault).default_variant).toBeTruthy()
  })

  test('validateExperimentForm enforces experiment variant and window bounds', () => {
    const base = emptyExperimentValues()
    expect(
      validateExperimentForm({ ...base, variants: [base.variants[0]!] }).variants,
    ).toBe('Add at least two variants')
    expect(
      validateExperimentForm({
        ...base,
        variants: Array.from({ length: 11 }, (_, index) => ({
          key: `variant-${index}`,
          weight: 1,
          description: '',
        })),
      }).variants,
    ).toBe('Experiments support at most 10 variants')
    expect(
      validateExperimentForm({
        ...base,
        variants: [
          { key: 'control', weight: 1, description: '' },
          { key: 'treatment', weight: 0, description: '' },
        ],
      }).variants,
    ).toBe('Every variant weight must be a positive integer')

    expect(
      validateExperimentForm({
        ...base,
        start_date: '2026-01-01',
        end_date: '2026-04-01',
      }).dates,
    ).toBeUndefined()
    expect(
      validateExperimentForm({
        ...base,
        start_date: '2026-01-01',
        end_date: '2026-04-02',
      }).dates,
    ).toBe('Experiment duration must not exceed 90 days')
  })

  test('validateExperimentForm rejects path-unsafe experiment and flag keys', () => {
    const base = emptyExperimentValues()
    expect(validateExperimentForm({ ...base, key: 'bad/key' }).key).toBeTruthy()
    expect(validateExperimentForm({ ...base, key: 'good.key-1', flagKey: 'bad key' }).flagKey)
      .toBeTruthy()
    expect(validateExperimentForm({ ...base, key: 'good.key-1', flagKey: 'flag_ok' }).key)
      .toBeUndefined()
  })
})

describe('ExperimentListPage', () => {
  test('renders experiments with status pills', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
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

  test('marks archived experiments as retained read-only records', async () => {
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [ARCHIVED_EXPERIMENT], count: 1 }),
      ),
    )
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
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
    expect(screen.getByText('archived')).toBeInTheDocument()
  })

  test('does not offer creation without config:write', async () => {
    const workspace: Workspace = seedWorkspace(makeWorkspace({ roles: ['config:read'] }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[workspace]}>
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
    expect(screen.queryByRole('link', { name: /new experiment/i })).not.toBeInTheDocument()
  })
})

describe('ExperimentDetailPage', () => {
  test('remounts form state and mutation identity when navigating between experiments', async () => {
    const secondExperiment = {
      ...EXPERIMENT,
      key: 'pricing-test',
      flag_key: 'pricing-test',
      description: 'Pricing experiment',
      version: 7,
    }
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [EXPERIMENT, secondExperiment], count: 2 }),
      ),
    )
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const router = createMemoryRouter(
      [
        { path: '/experiments/:key', element: <ExperimentDetailPage /> },
        { path: '/experiments', element: <div>experiment list</div> },
      ],
      { initialEntries: ['/experiments/checkout-test?tab=setup'] },
    )
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <RouterProvider router={router} />
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )

    await screen.findByDisplayValue('CTA experiment')
    await act(async () => {
      await router.navigate('/experiments/pricing-test?tab=setup')
    })

    const description = await screen.findByDisplayValue('Pricing experiment')
    expect(screen.queryByDisplayValue('CTA experiment')).not.toBeInTheDocument()
    await userEvent.clear(description)
    await userEvent.type(description, 'Updated pricing experiment')
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(updateBodies).toHaveLength(1))
    expect(updateKeys).toEqual(['pricing-test'])
    expect(updateBodies[0]).toEqual({
      version: 7,
      description: 'Updated pricing experiment',
    })
  })

  test('running to stopped omits every Config-frozen field from the update request', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MemoryRouter initialEntries={['/experiments/checkout-test?tab=setup']}>
              <Routes>
                <Route path="/experiments/:key" element={<ExperimentDetailPage />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )

    await screen.findByDisplayValue('CTA experiment')
    expect(screen.getByRole('spinbutton', { name: 'Traffic percentage' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toHaveValue(
      'anonymous_id',
    )
    expect(screen.getByRole('textbox', { name: 'Targeting rules JSON' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Control variant' })).toHaveValue('control')
    expect(
      screen.getByText(
        "Statistical control for every comparison and the backing flag's fallback variant.",
      ),
    ).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'stopped')
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(updateBodies).toHaveLength(1))
    expect(updateBodies[0]).toEqual({ version: 2, status: 'stopped' })
    for (const field of [
      'start_date',
      'end_date',
      'variants',
      'default_variant',
      'primary_metric',
      'statistical_plan',
    ]) {
      expect(updateBodies[0]).not.toHaveProperty(field)
    }
  })

  test('archives a launched experiment with archive-specific confirmation and toast', async () => {
    const success = vi.spyOn(toast, 'success')
    server.use(
      http.delete(
        '*/api/projects/demo/config/v1/admin/experiments/:key',
        ({ request, params }) => {
          deleteRequestUrl = request.url
          return HttpResponse.json({
            deleted: false,
            archived: true,
            key: String(params.key),
            flag_key: 'checkout-test',
            version: 3,
          })
        },
      ),
    )
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MemoryRouter initialEntries={['/experiments/checkout-test?tab=setup']}>
              <Routes>
                <Route path="/experiments/:key" element={<ExperimentDetailPage />} />
                <Route path="/experiments" element={<div>experiment list</div>} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )

    await screen.findByDisplayValue('CTA experiment')
    await userEvent.click(screen.getByRole('button', { name: 'Archive…' }))
    expect(screen.getByRole('heading', { name: 'Archive experiment "checkout-test"?' }))
      .toBeInTheDocument()
    expect(screen.getByText(/archives the experiment as an immutable record/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Archive' }))

    expect(await screen.findByText('experiment list')).toBeInTheDocument()
    expect(success).toHaveBeenCalledWith('Experiment "checkout-test" archived')
  })

  test('renders an archived experiment read-only without another removal action', async () => {
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [ARCHIVED_EXPERIMENT], count: 1 }),
      ),
    )
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[seedWorkspace()]}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MemoryRouter initialEntries={['/experiments/checkout-test?tab=setup']}>
              <Routes>
                <Route path="/experiments/:key" element={<ExperimentDetailPage />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )

    const description = await screen.findByDisplayValue('CTA experiment')
    expect(description).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^(save changes|archive|delete)/i }))
      .not.toBeInTheDocument()
    expect(screen.getByText(/by credential:operator/i)).toBeInTheDocument()
  })

  test('keeps a live experiment read-only without config:write', async () => {
    const workspace: Workspace = seedWorkspace(makeWorkspace({ roles: ['config:read'] }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <WorkspaceProvider initialWorkspaces={[workspace]}>
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <MemoryRouter initialEntries={['/experiments/checkout-test?tab=setup']}>
              <Routes>
                <Route path="/experiments/:key" element={<ExperimentDetailPage />} />
              </Routes>
            </MemoryRouter>
          </TooltipProvider>
        </QueryClientProvider>
      </WorkspaceProvider>,
    )

    expect(await screen.findByDisplayValue('CTA experiment')).toBeDisabled()
    expect(screen.queryByRole('button', { name: /^(save changes|archive|delete)/i }))
      .not.toBeInTheDocument()
    expect(updateBodies).toEqual([])
  })
})
