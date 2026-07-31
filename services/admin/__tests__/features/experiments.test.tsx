import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { useState } from 'react'
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
import { SUPPORTED_OPERATORS } from '../../src/core/evaluator/targetingContract'
import { WorkspaceProvider } from '../../src/core/workspace'
import type { Workspace } from '../../src/core/workspace'
import {
  ExperimentForm,
  buildCreate,
  buildUpdate,
  emptyExperimentValues,
  entryToFormValues,
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

// Same experiment before launch: Config freezes analysis-defining fields on the
// way out of draft, so the draft form is the one place they stay editable.
const DRAFT_EXPERIMENT = { ...EXPERIMENT, status: 'draft' }

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
    expect(
      experimentCreateSchema.safeParse({
        ...create,
        targeting_rules: Array.from({ length: 51 }, (_, index) => ({
          id: `rule-${index}`,
          name: '',
          conditions: [],
        })),
      }).success,
    ).toBe(false)
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
  test('projects structured targeting values to the strict eligibility payload', () => {
    const values = {
      ...emptyExperimentValues(),
      key: 'experiment-1',
      targetingRules: [
        {
          id: 'rule-eu',
          name: 'EU visitors',
          conditions: [
            {
              attribute: 'country',
              operator: 'equals' as const,
              valueType: 'string' as const,
              value: 'DE',
              values: [],
            },
            {
              attribute: 'email',
              operator: 'exists' as const,
              valueType: 'string' as const,
              value: 'ignored',
              values: [],
            },
            {
              attribute: 'age',
              operator: 'gte' as const,
              valueType: 'number' as const,
              value: '18',
              values: [],
            },
            {
              attribute: 'cohort',
              operator: 'in' as const,
              valueType: 'number' as const,
              value: '',
              values: [18, true, '18'],
            },
          ],
        },
      ],
    }

    expect(buildCreate(values).targeting_rules).toEqual([
      {
        id: 'rule-eu',
        name: 'EU visitors',
        conditions: [
          { attribute: 'country', operator: 'equals', value: 'DE' },
          { attribute: 'email', operator: 'exists' },
          { attribute: 'age', operator: 'gte', value: 18 },
          { attribute: 'cohort', operator: 'in', value: [18, true, '18'] },
        ],
      },
    ])
    expect(validateExperimentForm(values)).toEqual({})

    const invalid = {
      ...values,
      targetingRules: [
        {
          ...values.targetingRules[0]!,
          conditions: [
            {
              attribute: '',
              operator: 'equals' as const,
              valueType: 'string' as const,
              value: '',
              values: [],
            },
          ],
        },
      ],
    }
    expect(validateExperimentForm(invalid).targeting).toBeTruthy()
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
      targetingRules: [],
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

  test('entryToFormValues projects aware timestamps to native date input values', () => {
    const values = entryToFormValues(experimentEntrySchema.parse(EXPERIMENT))

    expect(values.start_date).toBe('2026-06-01')
    expect(values.end_date).toBe('2026-07-01')
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
    draftValues.default_variant = 'treatment'
    draftValues.start_date = '2026-06-01'
    draftValues.traffic_percentage = 50
    draftValues.targetingRules = [
      {
        id: 'rule-pro',
        name: 'Pro users',
        conditions: [
          {
            attribute: 'plan',
            operator: 'equals',
            valueType: 'string',
            value: 'pro',
            values: [],
          },
        ],
      },
    ]
    expect(buildUpdate(draftValues, draft, 7)).toEqual({
      version: 7,
      description: 'Changed',
      bucket_by: 'user_id',
      default_variant: 'treatment',
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
    stoppedValues.targetingRules = [
      {
        id: 'rule-pro',
        name: 'Pro users',
        conditions: [
          {
            attribute: 'plan',
            operator: 'equals',
            valueType: 'string',
            value: 'pro',
            values: [],
          },
        ],
      },
    ]

    expect(buildUpdate(stoppedValues, running)).toEqual({ version: 2, status: 'stopped' })
  })

  test('buildUpdate preserves unchanged draft scheduling timestamps', () => {
    const draft = experimentEntrySchema.parse(DRAFT_EXPERIMENT)
    const values = entryToFormValues(draft)
    values.description = 'Changed'

    expect(buildUpdate(values, draft)).toEqual({ version: 2, description: 'Changed' })
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

describe('ExperimentForm layout', () => {
  test('labels variant columns and keeps row-specific field names while adding and removing rows', async () => {
    function FormHarness() {
      const [values, setValues] = useState({
        ...emptyExperimentValues(),
        key: 'checkout-test',
      })

      return (
        <ExperimentForm
          values={values}
          onChange={setValues}
          isCreate
          onSubmit={vi.fn()}
          submitting={false}
        />
      )
    }

    render(<FormHarness />)

    const headings = within(screen.getByTestId('variant-column-headings'))
    expect(headings.getByText('Key')).toBeInTheDocument()
    expect(headings.getByText('User proportion')).toBeInTheDocument()
    expect(headings.getByText('Comment')).toBeInTheDocument()

    expect(screen.getByRole('textbox', { name: 'Key for variant 1' })).toHaveValue('control')
    expect(screen.getByRole('spinbutton', { name: 'User proportion for variant 1' })).toHaveValue(1)
    expect(screen.getByRole('textbox', { name: 'Comment for variant 1' })).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove variant 1' })).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: 'Add variant' }))

    expect(screen.getByRole('textbox', { name: 'Key for variant 3' })).toBeVisible()
    expect(screen.getByRole('spinbutton', { name: 'User proportion for variant 3' })).toHaveValue(1)
    expect(screen.getByRole('textbox', { name: 'Comment for variant 3' })).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove variant 3' })).toBeEnabled()

    await userEvent.click(screen.getByRole('button', { name: 'Remove variant 2' }))

    expect(screen.queryByRole('textbox', { name: 'Key for variant 3' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Key for variant 2' })).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Remove variant 2' })).toBeDisabled()
  })

  test('keeps primary create inputs visible and starts Advanced Settings collapsed', async () => {
    render(
      <ExperimentForm
        values={{ ...emptyExperimentValues(), key: 'checkout-test' }}
        onChange={vi.fn()}
        isCreate
        onSubmit={vi.fn()}
        submitting={false}
      />,
    )

    const summaryText = screen.getByText('Advanced Settings')
    const disclosure = summaryText.closest('details')
    expect(disclosure).not.toBeNull()
    expect(disclosure).not.toHaveAttribute('open')

    expect(screen.getByPlaceholderText('checkout-redesign')).toBeVisible()
    expect(screen.getByPlaceholderText('What this experiment tests')).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Control variant' })).not.toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).not.toBeVisible()
    const status = screen.getByRole('combobox', { name: 'Status' })
    const traffic = screen.getByRole('spinbutton', { name: 'Traffic percentage' })
    expect(status).toBeVisible()
    expect(status).toHaveValue('draft')
    expect(within(status).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'draft',
      'scheduled',
      'running',
    ])
    expect(traffic).toBeVisible()
    expect(traffic).toHaveValue(100)
    expect(traffic).toHaveAttribute('min', '0')
    expect(traffic).toHaveAttribute('max', '100')
    expect(screen.getAllByRole('combobox', { name: 'Status' })).toHaveLength(1)
    expect(screen.getAllByRole('spinbutton', { name: 'Traffic percentage' })).toHaveLength(1)
    expect(screen.getByText('Weights set the relative split among variants.')).toBeVisible()
    expect(
      screen.getByText('Enrollment, flag, scheduling, analysis, and targeting controls.'),
    ).toBeVisible()

    await userEvent.click(summaryText.closest('summary')!)

    expect(disclosure).toHaveAttribute('open')
    expect(screen.getByRole('combobox', { name: 'Control variant' })).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toBeVisible()
    expect(
      screen.getByText(
        "Statistical control for every comparison and the backing flag's fallback variant.",
      ),
    ).toBeVisible()
    expect(
      screen.getByText(
        'Immutable after draft. Use anonymous visitors for browser experiments that start before sign-in.',
      ),
    ).toBeVisible()
    expect(screen.getAllByRole('combobox', { name: 'Control variant' })).toHaveLength(1)
    expect(screen.getAllByRole('combobox', { name: 'Bucketing identity' })).toHaveLength(1)
    expect(within(disclosure!).queryByRole('combobox', { name: 'Status' })).not.toBeInTheDocument()
    expect(
      within(disclosure!).queryByRole('spinbutton', { name: 'Traffic percentage' }),
    ).not.toBeInTheDocument()
  })

  test('keeps edit controls visible when collapsed and preserves lifecycle locks', async () => {
    const runningValues = { ...emptyExperimentValues(), status: 'running' as const }
    const { rerender } = render(
      <ExperimentForm
        values={runningValues}
        onChange={vi.fn()}
        isCreate={false}
        currentStatus="running"
        onSubmit={vi.fn()}
        submitting={false}
      />,
    )

    const runningStatus = screen.getByRole('combobox', { name: 'Status' })
    expect(runningStatus).toBeEnabled()
    expect(within(runningStatus).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'running',
      'completed',
      'stopped',
    ])
    const runningTraffic = screen.getByRole('spinbutton', { name: 'Traffic percentage' })
    expect(runningTraffic).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Control variant' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toBeDisabled()

    const disclosure = screen.getByText('Advanced Settings').closest('details')
    expect(disclosure).toHaveAttribute('open')
    await userEvent.click(screen.getByText('Advanced Settings').closest('summary')!)
    expect(disclosure).not.toHaveAttribute('open')
    expect(runningStatus).toBeVisible()
    expect(runningTraffic).toBeVisible()

    rerender(
      <ExperimentForm
        values={{ ...runningValues, status: 'stopped' }}
        onChange={vi.fn()}
        isCreate={false}
        currentStatus="stopped"
        onSubmit={vi.fn()}
        submitting={false}
      />,
    )

    const stoppedStatus = screen.getByRole('combobox', { name: 'Status' })
    expect(stoppedStatus).toBeDisabled()
    expect(within(stoppedStatus).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'stopped',
    ])
    expect(screen.getByText('This experiment has ended — status is terminal.')).toBeVisible()
  })

  test('updates control options from variant keys and keeps strict bucketing options', async () => {
    function SelectorHarness() {
      const [values, setValues] = useState({
        ...emptyExperimentValues(),
        key: 'checkout-test',
      })

      return (
        <ExperimentForm
          values={values}
          onChange={setValues}
          isCreate
          onSubmit={vi.fn()}
          submitting={false}
        />
      )
    }

    render(<SelectorHarness />)
    await userEvent.click(screen.getByText('Advanced Settings').closest('summary')!)

    const control = screen.getByRole('combobox', { name: 'Control variant' })
    expect(within(control).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'control',
      'treatment',
    ])

    const bucketing = screen.getByRole('combobox', { name: 'Bucketing identity' })
    expect(
      within(bucketing).getAllByRole('option').map((option) => ({
        label: option.textContent,
        value: (option as HTMLOptionElement).value,
      })),
    ).toEqual([
      { label: 'Anonymous visitor', value: 'anonymous_id' },
      { label: 'Authenticated user', value: 'user_id' },
    ])

    const treatmentKey = screen.getByRole('textbox', { name: 'Key for variant 2' })
    await userEvent.clear(treatmentKey)
    await userEvent.type(treatmentKey, 'challenger')

    expect(within(control).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'control',
      'challenger',
    ])
  })

  test('opens Advanced Settings for hidden enrollment validation errors', async () => {
    render(
      <ExperimentForm
        values={{
          ...emptyExperimentValues(),
          key: 'checkout-test',
          default_variant: 'missing',
          bucket_by: 'account_id' as ExperimentFormValues['bucket_by'],
        }}
        onChange={vi.fn()}
        isCreate
        onSubmit={vi.fn()}
        submitting={false}
      />,
    )

    const disclosure = screen.getByText('Advanced Settings').closest('details')
    expect(disclosure).not.toHaveAttribute('open')

    await userEvent.click(screen.getByRole('button', { name: 'Create experiment' }))

    expect(disclosure).toHaveAttribute('open')
    expect(
      screen.getByText('Choose a control variant that matches a variant key'),
    ).toBeVisible()
    expect(screen.getByText('Choose anonymous_id or user_id')).toBeVisible()
  })

  test('opens Advanced Settings when a hidden launch requirement fails validation', async () => {
    const onSubmit = vi.fn()
    render(
      <ExperimentForm
        values={{
          ...emptyExperimentValues(),
          key: 'checkout-test',
          status: 'running',
        }}
        onChange={vi.fn()}
        isCreate
        onSubmit={onSubmit}
        submitting={false}
      />,
    )

    const disclosure = screen.getByText('Advanced Settings').closest('details')
    expect(disclosure).not.toHaveAttribute('open')

    await userEvent.click(screen.getByRole('button', { name: 'Create experiment' }))

    expect(disclosure).toHaveAttribute('open')
    expect(
      screen.getByText('Scheduled and running experiments require start and end dates'),
    ).toBeVisible()
    expect(
      screen.getByText('Scheduled and running experiments require a primary metric'),
    ).toBeVisible()
    expect(onSubmit).not.toHaveBeenCalled()
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

function renderDetail() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
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
}

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
    renderDetail()

    await screen.findByDisplayValue('CTA experiment')
    expect(screen.getByLabelText('Start date')).toHaveAttribute('type', 'date')
    expect(screen.getByLabelText('Start date')).toHaveValue('2026-06-01')
    expect(screen.getByLabelText('End date')).toHaveAttribute('type', 'date')
    expect(screen.getByLabelText('End date')).toHaveValue('2026-07-01')
    expect(screen.getByRole('spinbutton', { name: 'Traffic percentage' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Bucketing identity' })).toHaveValue(
      'anonymous_id',
    )
    expect(screen.getByRole('button', { name: 'Add targeting rule' })).toBeDisabled()
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

  test('uses structured targeting pickers and omits values for presence operators', async () => {
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [DRAFT_EXPERIMENT], count: 1 }),
      ),
    )
    renderDetail()

    await screen.findByDisplayValue('CTA experiment')
    expect(screen.queryByText(/targeting rules \(JSON array\)/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Targeting rules JSON' }))
      .not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Add targeting rule' }))
    const type = screen.getByRole('combobox', {
      name: 'Targeting rule 1 condition 1 type',
    })
    const operator = screen.getByRole('combobox', {
      name: 'Targeting rule 1 condition 1 operator',
    }) as HTMLSelectElement

    expect(new Set(Array.from(operator.options, (option) => option.value))).toEqual(
      SUPPORTED_OPERATORS,
    )
    await userEvent.type(type, 'email')
    await userEvent.selectOptions(operator, 'exists')
    expect(
      screen.queryByRole('textbox', { name: 'Targeting rule 1 condition 1 value' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText('No value — checks presence')).toBeInTheDocument()

    await userEvent.click(
      screen.getByRole('button', {
        name: 'Add condition after targeting rule 1 condition 1',
      }),
    )
    await userEvent.type(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 2 type' }),
      'age',
    )
    await userEvent.selectOptions(
      screen.getByRole('combobox', {
        name: 'Targeting rule 1 condition 2 operator',
      }),
      'gte',
    )
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Targeting rule 1 condition 2 value' }),
      '18',
    )

    await userEvent.click(
      screen.getByRole('button', {
        name: 'Add condition after targeting rule 1 condition 2',
      }),
    )
    await userEvent.type(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 3 type' }),
      'beta_opt_in',
    )
    await userEvent.selectOptions(
      screen.getByRole('combobox', {
        name: 'Targeting rule 1 condition 3 value type',
      }),
      'boolean',
    )
    await userEvent.selectOptions(
      screen.getByRole('combobox', {
        name: 'Targeting rule 1 condition 3 value',
      }),
      'true',
    )

    await userEvent.click(
      screen.getByRole('button', {
        name: 'Add condition after targeting rule 1 condition 3',
      }),
    )
    await userEvent.type(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 4 type' }),
      'cohort',
    )
    await userEvent.selectOptions(
      screen.getByRole('combobox', {
        name: 'Targeting rule 1 condition 4 operator',
      }),
      'in',
    )
    const membershipType = screen.getByRole('combobox', {
      name: 'Targeting rule 1 condition 4 values type',
    })
    await userEvent.selectOptions(membershipType, 'number')
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Targeting rule 1 condition 4 values' }),
      '18{Enter}',
    )
    await userEvent.selectOptions(membershipType, 'boolean')
    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 4 values' }),
      'true',
    )
    await userEvent.click(
      screen.getByRole('button', {
        name: 'Add targeting rule 1 condition 4 values',
      }),
    )
    await userEvent.selectOptions(membershipType, 'string')
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Targeting rule 1 condition 4 values' }),
      '18{Enter}',
    )

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(updateBodies).toHaveLength(1))
    const rules = updateBodies[0]!.targeting_rules as {
      conditions: Record<string, unknown>[]
    }[]
    expect(rules[0]!.conditions).toEqual([
      { attribute: 'email', operator: 'exists' },
      { attribute: 'age', operator: 'gte', value: 18 },
      { attribute: 'beta_opt_in', operator: 'equals', value: true },
      { attribute: 'cohort', operator: 'in', value: [18, true, '18'] },
    ])
    expect(rules[0]!.conditions[0]).not.toHaveProperty('value')
  })

  test('a draft leaves every analysis-defining field editable and submits the edits', async () => {
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [DRAFT_EXPERIMENT], count: 1 }),
      ),
    )
    renderDetail()

    await screen.findByDisplayValue('CTA experiment')

    // The complement of the running case above: nothing is frozen while the
    // persisted status is still draft.
    const traffic = screen.getByRole('spinbutton', { name: 'Traffic percentage' })
    expect(traffic).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Add targeting rule' })).toBeEnabled()
    expect(screen.getByRole('textbox', { name: 'Metric event' })).toBeEnabled()
    expect(screen.getByRole('combobox', { name: 'Metric direction' })).toBeEnabled()
    expect(screen.getByRole('spinbutton', { name: 'Baseline conversion rate' })).toBeEnabled()

    await userEvent.clear(traffic)
    await userEvent.type(traffic, '55')
    await userEvent.click(screen.getByRole('button', { name: 'Add targeting rule' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Targeting rule 1 name' }), 'EU')
    await userEvent.type(
      screen.getByRole('combobox', { name: 'Targeting rule 1 condition 1 type' }),
      'country',
    )
    await userEvent.type(
      screen.getByRole('textbox', { name: 'Targeting rule 1 condition 1 value' }),
      'DE',
    )
    await userEvent.clear(screen.getByPlaceholderText('What this experiment tests'))
    await userEvent.type(screen.getByPlaceholderText('What this experiment tests'), 'Draft copy')
    await userEvent.clear(screen.getByRole('textbox', { name: 'Metric event' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Metric event' }), 'signup')
    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Metric direction' }),
      'decrease',
    )
    const baseline = screen.getByRole('spinbutton', { name: 'Baseline conversion rate' })
    await userEvent.clear(baseline)
    await userEvent.type(baseline, '0.4')
    const mde = screen.getByRole('spinbutton', { name: 'Minimum detectable effect' })
    await userEvent.clear(mde)
    await userEvent.type(mde, '0.3')

    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(updateBodies).toHaveLength(1))
    expect(updateBodies[0]).toMatchObject({
      version: 2,
      description: 'Draft copy',
      traffic_percentage: 55,
      targeting_rules: [
        {
          name: 'EU',
          conditions: [{ attribute: 'country', operator: 'equals', value: 'DE' }],
        },
      ],
    })
    const targetingRules = updateBodies[0]!.targeting_rules as {
      id: string
      rollout?: unknown
      conditions: { type?: unknown }[]
    }[]
    expect(targetingRules[0]!.id).toMatch(/^rule_[a-f0-9]{12}$/)
    expect(targetingRules[0]).not.toHaveProperty('rollout')
    expect(targetingRules[0]!.conditions[0]).not.toHaveProperty('type')
    expect(updateBodies[0]).toHaveProperty('primary_metric')
    expect(updateBodies[0]).toHaveProperty('statistical_plan')
  })

  test('choosing a launch status does not freeze the fields before the draft is saved', async () => {
    server.use(
      http.get('*/api/projects/demo/config/v1/admin/experiments', () =>
        HttpResponse.json({ experiments: [DRAFT_EXPERIMENT], count: 1 }),
      ),
    )
    renderDetail()

    await screen.findByDisplayValue('CTA experiment')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'running')

    // The lock keys off the persisted status, not the pending selection, so the
    // launch form can still set final traffic and targeting in the same save.
    expect(screen.getByRole('spinbutton', { name: 'Traffic percentage' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Add targeting rule' })).toBeEnabled()

    const traffic = screen.getByRole('spinbutton', { name: 'Traffic percentage' })
    await userEvent.clear(traffic)
    await userEvent.type(traffic, '25')
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => expect(updateBodies).toHaveLength(1))
    expect(updateBodies[0]).toMatchObject({ status: 'running', traffic_percentage: 25 })
  })
})
