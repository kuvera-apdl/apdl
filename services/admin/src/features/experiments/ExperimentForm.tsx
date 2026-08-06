// Experiment setup form (gap G5): a structured editor over the canonical record.
// An experiment owns a backing flag, so variants/default_variant/traffic map to
// the flag and status drives flag serving through lifecycle-aware transitions.
import { Plus, Trash2 } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { z } from 'zod'

import {
  experimentBucketBySchema,
  experimentCreateStatusSchema,
  experimentPathKeySchema,
  experimentTargetingRuleSchema,
} from '@/api/schemas/experiments'
import type {
  ExperimentBucketBy,
  ExperimentCreate,
  ExperimentEntry,
  ExperimentMetric,
  ExperimentStatisticalPlan,
  ExperimentStatus,
  ExperimentUpdate,
  ExperimentVariant,
} from '@/api/types/experiments'
import { Button } from '@/components/ui/button'
import { Disclosure } from '@/components/ui/disclosure'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { MAX_RULES } from '@/core/evaluator/targetingContract'

import {
  ExperimentTargetingRules,
  targetingRulesToFormValues,
  targetingRulesToWire,
  type ExperimentTargetingRuleFormValue,
} from './ExperimentTargetingRules'

// Mirrors the Config service _ALLOWED_STATUS_TRANSITIONS: completed/stopped are
// terminal (no resume).
const CREATE_STATUSES: ExperimentStatus[] = ['draft', 'scheduled', 'running']
const STATUS_TRANSITIONS: Record<ExperimentStatus, ExperimentStatus[]> = {
  draft: ['draft', 'scheduled', 'running', 'stopped'],
  scheduled: ['scheduled', 'running', 'stopped'],
  running: ['running', 'completed', 'stopped'],
  completed: ['completed'],
  stopped: ['stopped'],
}

const METRIC_DIRECTIONS = ['increase', 'decrease'] as const
const MAX_EXPERIMENT_VARIANTS = 10
const MAX_EXPERIMENT_DURATION_MS = 90 * 24 * 60 * 60 * 1000
const UTC_DATE_TIME_INPUT_PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/

const utcDateTimeInputSchema = z
  .string()
  .regex(UTC_DATE_TIME_INPUT_PATTERN)
  .refine((value) => {
    const instant = new Date(`${value}Z`)
    return (
      !Number.isNaN(instant.getTime()) && instant.toISOString().slice(0, 19) === value
    )
  })

const optionalUtcDateTimeInputSchema = z.union([
  z.literal('').transform(() => null),
  utcDateTimeInputSchema.transform((value) => `${value}Z`),
])

function toUtcDateTimeInputValue(value: string | null): string {
  if (value === null) return ''
  return new Date(value).toISOString().slice(0, 19)
}

function normalizeUtcDateTimeInputValue(value: string): string {
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value) ? `${value}:00` : value
}

export interface ExperimentVariantRow {
  key: string
  weight: number
  description: string
}

export interface ExperimentFormValues {
  key: string
  flagKey: string
  status: ExperimentStatus
  description: string
  bucket_by: ExperimentBucketBy
  traffic_percentage: number
  start_date: string
  end_date: string
  variants: ExperimentVariantRow[]
  default_variant: string
  metricEvent: string
  metricDirection: ExperimentMetric['direction']
  baselineConversionRate: number
  minimumDetectableEffect: number
  significanceLevel: number
  nominalPower: number
  requiredSampleSizePerArm: number
  dataSettlementSeconds: number
  targetingRules: ExperimentTargetingRuleFormValue[]
}

export function emptyExperimentValues(): ExperimentFormValues {
  return {
    key: '',
    flagKey: '',
    status: 'draft',
    description: '',
    bucket_by: 'anonymous_id',
    traffic_percentage: 100,
    start_date: '',
    end_date: '',
    variants: [
      { key: 'control', weight: 1, description: '' },
      { key: 'treatment', weight: 1, description: '' },
    ],
    default_variant: 'control',
    metricEvent: '',
    metricDirection: 'increase',
    baselineConversionRate: 0.1,
    minimumDetectableEffect: 0.02,
    significanceLevel: 0.05,
    nominalPower: 0.8,
    requiredSampleSizePerArm: 5000,
    dataSettlementSeconds: 300,
    targetingRules: [],
  }
}

export function entryToFormValues(entry: ExperimentEntry): ExperimentFormValues {
  return {
    key: entry.key,
    flagKey: entry.flag_key,
    status: entry.status,
    description: entry.description,
    bucket_by: entry.bucket_by,
    traffic_percentage: entry.traffic_percentage,
    start_date: toUtcDateTimeInputValue(entry.start_date),
    end_date: toUtcDateTimeInputValue(entry.end_date),
    variants: entry.variants.map((variant) => ({
      key: variant.key,
      weight: variant.weight,
      description: variant.description ?? '',
    })),
    default_variant: entry.default_variant,
    metricEvent: entry.primary_metric?.event ?? '',
    metricDirection: entry.primary_metric?.direction ?? 'increase',
    baselineConversionRate: entry.statistical_plan?.baseline_conversion_rate ?? 0.1,
    minimumDetectableEffect: entry.statistical_plan?.minimum_detectable_effect ?? 0.02,
    significanceLevel: entry.statistical_plan?.significance_level ?? 0.05,
    nominalPower: entry.statistical_plan?.nominal_power ?? 0.8,
    requiredSampleSizePerArm: entry.statistical_plan?.required_sample_size_per_arm ?? 5000,
    dataSettlementSeconds: entry.statistical_plan?.data_settlement_seconds ?? 300,
    targetingRules: targetingRulesToFormValues(entry.targeting_rules),
  }
}

function projectVariants(rows: ExperimentVariantRow[]): ExperimentVariant[] {
  return rows.map((row) => {
    const variant: ExperimentVariant = { key: row.key.trim(), weight: row.weight }
    if (row.description.trim() !== '') variant.description = row.description
    return variant
  })
}

function buildMetric(values: ExperimentFormValues): ExperimentMetric | null {
  if (values.metricEvent.trim() === '') return null
  return {
    event: values.metricEvent.trim(),
    type: 'conversion',
    direction: values.metricDirection,
  }
}

function buildStatisticalPlan(values: ExperimentFormValues): ExperimentStatisticalPlan {
  return {
    protocol: 'fixed_horizon_fisher_newcombe_cc_plan_v1',
    baseline_conversion_rate: values.baselineConversionRate,
    minimum_detectable_effect: values.minimumDetectableEffect,
    significance_level: values.significanceLevel,
    nominal_power: values.nominalPower,
    required_sample_size_per_arm: values.requiredSampleSizePerArm,
    data_settlement_seconds: values.dataSettlementSeconds,
  }
}

function toAwareDateTime(value: string): string | null {
  return optionalUtcDateTimeInputSchema.parse(value)
}

export function buildCreate(values: ExperimentFormValues): ExperimentCreate {
  const create: ExperimentCreate = {
    key: values.key.trim(),
    flag_key: values.flagKey.trim() || values.key.trim(),
    status: experimentCreateStatusSchema.parse(values.status),
    description: values.description,
    bucket_by: values.bucket_by,
    traffic_percentage: values.traffic_percentage,
    start_date: toAwareDateTime(values.start_date),
    end_date: toAwareDateTime(values.end_date),
    variants: projectVariants(values.variants),
    default_variant: values.default_variant,
    targeting_rules: targetingRulesToWire(values.targetingRules),
  }
  const metric = buildMetric(values)
  if (metric) {
    create.primary_metric = metric
    create.statistical_plan = buildStatisticalPlan(values)
  }
  return create
}

const same = (left: unknown, right: unknown): boolean =>
  JSON.stringify(left) === JSON.stringify(right)

export function buildUpdate(
  values: ExperimentFormValues,
  base: ExperimentEntry,
  version: number = base.version,
): ExperimentUpdate {
  const update: ExperimentUpdate = { version }

  if (values.status !== base.status) update.status = values.status
  if (values.description !== base.description) update.description = values.description

  // Config freezes analysis-defining fields as soon as an experiment leaves
  // draft. Merely echoing their current values still counts as an attempted
  // mutation, so non-draft updates must omit them entirely.
  if (base.status === 'draft') {
    if (values.bucket_by !== base.bucket_by) update.bucket_by = values.bucket_by
    if (values.traffic_percentage !== base.traffic_percentage) {
      update.traffic_percentage = values.traffic_percentage
    }

    const targetingRules = targetingRulesToWire(values.targetingRules)
    if (!same(targetingRules, base.targeting_rules)) update.targeting_rules = targetingRules

    const startDate = toAwareDateTime(values.start_date)
    const endDate = toAwareDateTime(values.end_date)
    const variants = projectVariants(values.variants)
    const primaryMetric = buildMetric(values)
    const statisticalPlan = primaryMetric ? buildStatisticalPlan(values) : null

    if (values.start_date !== toUtcDateTimeInputValue(base.start_date)) {
      update.start_date = startDate
    }
    if (values.end_date !== toUtcDateTimeInputValue(base.end_date)) {
      update.end_date = endDate
    }
    if (!same(variants, base.variants)) update.variants = variants
    if (values.default_variant !== base.default_variant) {
      update.default_variant = values.default_variant
    }
    if (!same(primaryMetric, base.primary_metric)) update.primary_metric = primaryMetric
    if (!same(statisticalPlan, base.statistical_plan)) {
      update.statistical_plan = statisticalPlan
    }
  }

  return update
}

interface ExperimentFormErrors {
  key?: string
  flagKey?: string
  bucket_by?: string
  variants?: string
  default_variant?: string
  targeting?: string
  dates?: string
  metric?: string
  statisticalPlan?: string
}

export function validateExperimentForm(values: ExperimentFormValues): ExperimentFormErrors {
  const errors: ExperimentFormErrors = {}
  const key = values.key.trim()
  const flagKey = values.flagKey.trim()
  if (!experimentPathKeySchema.safeParse(key).success) {
    errors.key = 'Use 1–128 letters, numbers, dots, underscores, or hyphens'
  }
  if (flagKey !== '' && !experimentPathKeySchema.safeParse(flagKey).success) {
    errors.flagKey = 'Use 1–128 letters, numbers, dots, underscores, or hyphens'
  }
  if (!experimentBucketBySchema.safeParse(values.bucket_by).success) {
    errors.bucket_by = 'Choose anonymous_id or user_id'
  }
  const keys = values.variants.map((variant) => variant.key.trim())
  if (values.variants.length < 2) errors.variants = 'Add at least two variants'
  else if (values.variants.length > MAX_EXPERIMENT_VARIANTS)
    errors.variants = `Experiments support at most ${MAX_EXPERIMENT_VARIANTS} variants`
  else if (keys.some((key) => key === '')) errors.variants = 'Every variant needs a key'
  else if (new Set(keys).size !== keys.length) errors.variants = 'Variant keys must be unique'
  else if (values.variants.some((variant) => !Number.isInteger(variant.weight) || variant.weight <= 0))
    errors.variants = 'Every variant weight must be a positive integer'

  if (!keys.includes(values.default_variant)) {
    errors.default_variant = 'Choose a control variant that matches a variant key'
  }

  const rules = z
    .array(experimentTargetingRuleSchema)
    .max(MAX_RULES)
    .safeParse(targetingRulesToWire(values.targetingRules))
  if (!rules.success) {
    errors.targeting = 'Each targeting condition needs a valid type, operator, and value'
  }

  const startResult = optionalUtcDateTimeInputSchema.safeParse(values.start_date)
  const endResult = optionalUtcDateTimeInputSchema.safeParse(values.end_date)
  const start = startResult.success ? startResult.data : null
  const end = endResult.success ? endResult.data : null
  if (!startResult.success || !endResult.success) {
    errors.dates = 'Use YYYY-MM-DDTHH:mm:ss in UTC'
  } else if (end !== null && start === null) {
    errors.dates = 'End date requires a start date'
  } else if (start !== null && end !== null && Date.parse(end) <= Date.parse(start)) {
    errors.dates = 'End date must be after start date'
  } else if (
    start !== null &&
    end !== null &&
    Date.parse(end) - Date.parse(start) > MAX_EXPERIMENT_DURATION_MS
  ) {
    errors.dates = 'Experiment duration must not exceed 90 days'
  }

  if (
    !errors.dates &&
    (values.status === 'scheduled' || values.status === 'running') &&
    (start === null || end === null)
  ) {
    errors.dates = 'Scheduled and running experiments require start and end dates'
  }
  if (
    (values.status === 'scheduled' || values.status === 'running') &&
    values.metricEvent.trim() === ''
  ) {
    errors.metric = 'Scheduled and running experiments require a primary metric'
  }
  if (
    (values.status === 'scheduled' || values.status === 'running') &&
    (
      !Number.isFinite(values.baselineConversionRate) ||
      values.baselineConversionRate < 0 ||
      values.baselineConversionRate > 1 ||
      !Number.isFinite(values.minimumDetectableEffect) ||
      values.minimumDetectableEffect < 1e-6 ||
      values.minimumDetectableEffect > 1 ||
      !Number.isFinite(values.significanceLevel) ||
      values.significanceLevel < 1e-6 ||
      values.significanceLevel > 0.5 ||
      !Number.isFinite(values.nominalPower) ||
      values.nominalPower <= 0.5 ||
      values.nominalPower > 0.9999 ||
      !Number.isInteger(values.requiredSampleSizePerArm) ||
      values.requiredSampleSizePerArm < 2 ||
      values.requiredSampleSizePerArm > 10_000_000 ||
      !Number.isInteger(values.dataSettlementSeconds) ||
      values.dataSettlementSeconds < 1 ||
      values.dataSettlementSeconds > 86_400
    )
  ) {
    errors.statisticalPlan = 'Enter a valid fixed-horizon plan and an integer target of at least 2 per arm'
  }
  return errors
}

export interface ExperimentFormProps {
  values: ExperimentFormValues
  onChange: (next: ExperimentFormValues) => void
  isCreate: boolean
  currentStatus?: ExperimentStatus
  onSubmit: () => void
  submitting: boolean
  keyError?: string | null
  readOnly?: boolean
}

export function ExperimentForm({
  values,
  onChange,
  isCreate,
  currentStatus,
  onSubmit,
  submitting,
  keyError,
  readOnly = false,
}: ExperimentFormProps) {
  const [errors, setErrors] = useState<ExperimentFormErrors>({})
  const advancedSettingsRef = useRef<HTMLDivElement>(null)
  const variantFieldsId = useId()
  const set = (patch: Partial<ExperimentFormValues>) => onChange({ ...values, ...patch })

  const setVariant = (index: number, patch: Partial<ExperimentVariantRow>) =>
    set({ variants: values.variants.map((variant, i) => (i === index ? { ...variant, ...patch } : variant)) })
  const addVariant = () =>
    set({ variants: [...values.variants, { key: '', weight: 1, description: '' }] })
  const removeVariant = (index: number) =>
    set({ variants: values.variants.filter((_, i) => i !== index) })

  const statusOptions = isCreate ? CREATE_STATUSES : STATUS_TRANSITIONS[currentStatus ?? values.status]
  const terminal = !isCreate && statusOptions.length <= 1
  const variantKeys = values.variants.map((variant) => variant.key.trim()).filter((key) => key !== '')
  const analysisFieldsLocked = !isCreate && currentStatus !== 'draft'

  const submit = () => {
    const nextErrors = validateExperimentForm(values)
    setErrors(nextErrors)
    if (Object.keys(nextErrors).length > 0) {
      if (
        nextErrors.flagKey ||
        nextErrors.bucket_by ||
        nextErrors.default_variant ||
        nextErrors.targeting ||
        nextErrors.dates ||
        nextErrors.metric ||
        nextErrors.statisticalPlan
      ) {
        const disclosure = advancedSettingsRef.current?.querySelector('details')
        if (disclosure) disclosure.open = true
      }
      return
    }
    onSubmit()
  }

  return (
    <fieldset className="max-w-2xl space-y-5" disabled={readOnly}>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Key</Label>
          <Input
            value={values.key}
            onChange={(event) => set({ key: event.target.value })}
            disabled={!isCreate}
            placeholder="checkout-redesign"
            className="font-mono text-xs"
          />
          {keyError || errors.key ? (
            <p className="text-xs text-destructive">{keyError ?? errors.key}</p>
          ) : null}
        </div>
        <div className="space-y-1.5">
          <Label>Description</Label>
          <Input
            value={values.description}
            onChange={(event) => set({ description: event.target.value })}
            placeholder="What this experiment tests"
          />
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Status</Label>
          <Select
            value={values.status}
            onChange={(event) => set({ status: event.target.value as ExperimentStatus })}
            disabled={terminal}
            aria-label="Status"
          >
            {statusOptions.map((status) => (
              <option key={status} value={status}>
                {status}
              </option>
            ))}
          </Select>
          <p className="text-xs text-muted-foreground">
            {terminal
              ? 'This experiment has ended — status is terminal.'
              : 'Defaults to draft. Running enables the backing flag.'}
          </p>
        </div>
        <div className="space-y-1.5">
          <Label>Traffic %</Label>
          <Input
            type="number"
            min={0}
            max={100}
            step="any"
            value={values.traffic_percentage}
            onChange={(event) =>
              set({
                traffic_percentage: Math.min(
                  100,
                  Math.max(0, Number(event.target.value) || 0),
                ),
              })
            }
            disabled={analysisFieldsLocked}
            aria-label="Traffic percentage"
            className="tabular-nums"
          />
          <p className="text-xs text-muted-foreground">
            Defaults to all eligible actors.
          </p>
        </div>
      </div>

      <div className="space-y-2">
        <Label>Variants</Label>
        <div className="space-y-2">
          <div
            className="hidden gap-2 sm:grid sm:grid-cols-[10rem_9rem_minmax(11rem,1fr)_2.25rem]"
            data-testid="variant-column-headings"
          >
            <span className="text-sm font-medium leading-none">Key</span>
            <span className="text-sm font-medium leading-none">User proportion</span>
            <span className="text-sm font-medium leading-none">Comment</span>
            <span aria-hidden="true" />
          </div>
          {values.variants.map((variant, index) => (
            <div
              key={index}
              className="grid gap-2 sm:grid-cols-[10rem_9rem_minmax(11rem,1fr)_2.25rem] sm:items-end"
            >
              <div className="space-y-1.5">
                <Label htmlFor={`${variantFieldsId}-${index}-key`} className="sm:sr-only">
                  <span aria-hidden="true">Key</span>
                  <span className="sr-only">Key for variant {index + 1}</span>
                </Label>
                <Input
                  id={`${variantFieldsId}-${index}-key`}
                  value={variant.key}
                  onChange={(event) => setVariant(index, { key: event.target.value })}
                  placeholder="key"
                  className="font-mono text-xs"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${variantFieldsId}-${index}-weight`} className="sm:sr-only">
                  <span aria-hidden="true">User proportion</span>
                  <span className="sr-only">User proportion for variant {index + 1}</span>
                </Label>
                <Input
                  id={`${variantFieldsId}-${index}-weight`}
                  type="number"
                  min={1}
                  step={1}
                  value={variant.weight}
                  onChange={(event) =>
                    setVariant(index, { weight: Math.max(1, Math.floor(Number(event.target.value) || 1)) })
                  }
                  className="tabular-nums"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${variantFieldsId}-${index}-description`} className="sm:sr-only">
                  <span aria-hidden="true">Comment</span>
                  <span className="sr-only">Comment for variant {index + 1}</span>
                </Label>
                <Input
                  id={`${variantFieldsId}-${index}-description`}
                  value={variant.description}
                  onChange={(event) => setVariant(index, { description: event.target.value })}
                  placeholder="description (optional)"
                />
              </div>
              <div className="sm:self-end">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => removeVariant(index)}
                  aria-label={`Remove variant ${index + 1}`}
                  disabled={values.variants.length <= 2}
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
        </div>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={addVariant}
          disabled={values.variants.length >= MAX_EXPERIMENT_VARIANTS}
        >
          <Plus />
          Add variant
        </Button>
        {errors.variants ? <p className="text-xs text-destructive">{errors.variants}</p> : null}
        <p className="text-xs text-muted-foreground">
          Weights set the relative split among variants.
        </p>
      </div>

      <div ref={advancedSettingsRef}>
        <Disclosure
          summary={
            <span>
              <span className="block font-medium">Advanced Settings</span>
              <span className="block text-xs text-muted-foreground">
                Enrollment, flag, scheduling, analysis, and targeting controls.
              </span>
            </span>
          }
          defaultOpen={!isCreate}
        >
          <div className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Control variant</Label>
                <Select
                  value={values.default_variant}
                  onChange={(event) => set({ default_variant: event.target.value })}
                  disabled={analysisFieldsLocked}
                  aria-label="Control variant"
                >
                  {variantKeys.length === 0 ? <option value="">—</option> : null}
                  {variantKeys.map((key) => (
                    <option key={key} value={key}>
                      {key}
                    </option>
                  ))}
                </Select>
                {errors.default_variant ? (
                  <p className="text-xs text-destructive">{errors.default_variant}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    Statistical control for every comparison and the backing flag&apos;s fallback variant.
                  </p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label>Bucketing identity</Label>
                <Select
                  value={values.bucket_by}
                  onChange={(event) => set({ bucket_by: event.target.value as ExperimentBucketBy })}
                  disabled={analysisFieldsLocked}
                  aria-label="Bucketing identity"
                >
                  <option value="anonymous_id">Anonymous visitor</option>
                  <option value="user_id">Authenticated user</option>
                </Select>
                {errors.bucket_by ? (
                  <p className="text-xs text-destructive">{errors.bucket_by}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    Immutable after draft. Use anonymous visitors for browser experiments that start
                    before sign-in.
                  </p>
                )}
              </div>
            </div>

            <div className="space-y-1.5">
              <Label>Flag key</Label>
              <Input
                value={values.flagKey}
                onChange={(event) => set({ flagKey: event.target.value })}
                disabled={!isCreate}
                placeholder={values.key || 'defaults to key'}
                className="font-mono text-xs"
              />
              <p className="text-xs text-muted-foreground">
                Backing flag whose exposures measure this experiment. Defaults to the experiment
                key and is immutable once created.
              </p>
              {errors.flagKey ? <p className="text-xs text-destructive">{errors.flagKey}</p> : null}
            </div>

            <div className="space-y-1.5">
              <Label>Primary metric</Label>
              <div className="grid gap-2 sm:grid-cols-3">
                <Input
                  value={values.metricEvent}
                  onChange={(event) => set({ metricEvent: event.target.value })}
                  placeholder="event (e.g. purchase_completed)"
                  aria-label="Metric event"
                  disabled={analysisFieldsLocked}
                  className="font-mono text-xs"
                />
                <Input value="conversion" disabled aria-label="Metric type" />
                <Select
                  value={values.metricDirection}
                  onChange={(event) => set({ metricDirection: event.target.value as ExperimentMetric['direction'] })}
                  aria-label="Metric direction"
                  disabled={analysisFieldsLocked}
                >
                  {METRIC_DIRECTIONS.map((direction) => (
                    <option key={direction} value={direction}>
                      {direction}
                    </option>
                  ))}
                </Select>
              </div>
              <p className="text-xs text-muted-foreground">
                Optional — leave the event blank to skip. Only the event drives results.
              </p>
              {errors.metric ? <p className="text-xs text-destructive">{errors.metric}</p> : null}
            </div>
            <div className="space-y-3 rounded-md border p-4">
              <div>
                <Label>Fixed-horizon statistical plan</Label>
                <p className="text-xs text-muted-foreground">
                  Immutable after draft. Config validates the prospective per-arm target using the metric
                  direction and Bonferroni adjustment for every treatment arm.
                </p>
              </div>
              <Input value="fixed_horizon_fisher_newcombe_cc_plan_v1" disabled aria-label="Statistical protocol" />
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <div className="space-y-1.5">
                  <Label>Baseline conversion</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step="any"
                    value={values.baselineConversionRate}
                    onChange={(event) => set({ baselineConversionRate: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Baseline conversion rate"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Minimum detectable effect</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step="any"
                    value={values.minimumDetectableEffect}
                    onChange={(event) => set({ minimumDetectableEffect: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Minimum detectable effect"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Significance level</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step="any"
                    value={values.significanceLevel}
                    onChange={(event) => set({ significanceLevel: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Significance level"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Nominal power</Label>
                  <Input
                    type="number"
                    min={0}
                    max={1}
                    step="any"
                    value={values.nominalPower}
                    onChange={(event) => set({ nominalPower: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Nominal power"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Required actors / arm</Label>
                  <Input
                    type="number"
                    min={2}
                    step={1}
                    value={values.requiredSampleSizePerArm}
                    onChange={(event) => set({ requiredSampleSizePerArm: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Required sample size per arm"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Settlement hold (seconds)</Label>
                  <Input
                    type="number"
                    min={1}
                    max={86_400}
                    step={1}
                    value={values.dataSettlementSeconds}
                    onChange={(event) => set({ dataSettlementSeconds: Number(event.target.value) })}
                    disabled={analysisFieldsLocked}
                    aria-label="Data settlement seconds"
                  />
                </div>
              </div>
              {errors.statisticalPlan ? (
                <p className="text-xs text-destructive">{errors.statisticalPlan}</p>
              ) : null}
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="experiment-start-date">Start (UTC)</Label>
                <Input
                  id="experiment-start-date"
                  type="datetime-local"
                  step={1}
                  value={values.start_date}
                  onChange={(event) =>
                    set({ start_date: normalizeUtcDateTimeInputValue(event.target.value) })
                  }
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="experiment-end-date">End (UTC)</Label>
                <Input
                  id="experiment-end-date"
                  type="datetime-local"
                  step={1}
                  value={values.end_date}
                  onChange={(event) =>
                    set({ end_date: normalizeUtcDateTimeInputValue(event.target.value) })
                  }
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              Times are shown and saved in UTC.
            </p>
            {errors.dates ? <p className="text-xs text-destructive">{errors.dates}</p> : null}

            <div className="space-y-1.5">
              <ExperimentTargetingRules
                value={values.targetingRules}
                onChange={(targetingRules) => set({ targetingRules })}
                disabled={analysisFieldsLocked}
              />
              {errors.targeting ? <p className="text-xs text-destructive">{errors.targeting}</p> : null}
            </div>
          </div>
        </Disclosure>
      </div>

      {!readOnly ? (
        <Button onClick={submit} disabled={submitting || values.key.trim() === ''}>
          {isCreate ? 'Create experiment' : 'Save changes'}
        </Button>
      ) : null}
    </fieldset>
  )
}
