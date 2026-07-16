// Experiment setup form (gap G5): a structured editor over the canonical record.
// An experiment owns a backing flag, so variants/default_variant/traffic map to
// the flag and status drives flag serving through lifecycle-aware transitions.
// Targeting stays a JSON editor but is validated against the canonical GateRule
// schema rather than left raw.
import { Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { z } from 'zod'

import { gateRuleSchema } from '@/api/schemas/flags'
import {
  experimentCreateStatusSchema,
  experimentPathKeySchema,
} from '@/api/schemas/experiments'
import type { GateRule } from '@/api/types/flags'
import type {
  ExperimentCreate,
  ExperimentEntry,
  ExperimentMetric,
  ExperimentStatisticalPlan,
  ExperimentStatus,
  ExperimentUpdate,
  ExperimentVariant,
} from '@/api/types/experiments'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

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
  targetingRulesJson: string
}

export function emptyExperimentValues(): ExperimentFormValues {
  return {
    key: '',
    flagKey: '',
    status: 'draft',
    description: '',
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
    targetingRulesJson: '',
  }
}

export function entryToFormValues(entry: ExperimentEntry): ExperimentFormValues {
  return {
    key: entry.key,
    flagKey: entry.flag_key,
    status: entry.status,
    description: entry.description,
    traffic_percentage: entry.traffic_percentage,
    start_date: entry.start_date ?? '',
    end_date: entry.end_date ?? '',
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
    targetingRulesJson:
      entry.targeting_rules.length > 0 ? JSON.stringify(entry.targeting_rules, null, 2) : '',
  }
}

export function parseTargetingRules(raw: string): { value: GateRule[] | null; error: string | null } {
  if (raw.trim() === '') return { value: [], error: null }
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return { value: null, error: 'Invalid JSON' }
  }
  if (!Array.isArray(parsed)) return { value: null, error: 'Must be a JSON array of rules' }
  const result = z.array(gateRuleSchema).safeParse(parsed)
  if (!result.success) {
    return { value: null, error: 'Each rule needs id, name, conditions, and a rollout' }
  }
  return { value: result.data, error: null }
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
  const trimmed = value.trim()
  if (trimmed === '') return null
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return `${trimmed}T00:00:00Z`
  return trimmed
}

function isAwareDateTime(value: string | null): boolean {
  return value === null || (
    /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && !Number.isNaN(Date.parse(value))
  )
}

export function buildCreate(values: ExperimentFormValues): ExperimentCreate {
  const create: ExperimentCreate = {
    key: values.key.trim(),
    flag_key: values.flagKey.trim() || values.key.trim(),
    status: experimentCreateStatusSchema.parse(values.status),
    description: values.description,
    traffic_percentage: values.traffic_percentage,
    start_date: toAwareDateTime(values.start_date),
    end_date: toAwareDateTime(values.end_date),
    variants: projectVariants(values.variants),
    default_variant: values.default_variant,
    targeting_rules: parseTargetingRules(values.targetingRulesJson).value ?? [],
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
  if (values.traffic_percentage !== base.traffic_percentage) {
    update.traffic_percentage = values.traffic_percentage
  }

  const targetingRules = parseTargetingRules(values.targetingRulesJson).value ?? []
  if (!same(targetingRules, base.targeting_rules)) update.targeting_rules = targetingRules

  // Config freezes analysis-defining fields as soon as an experiment leaves
  // draft. Merely echoing their current values still counts as an attempted
  // mutation, so non-draft updates must omit them entirely.
  if (base.status === 'draft') {
    const startDate = toAwareDateTime(values.start_date)
    const endDate = toAwareDateTime(values.end_date)
    const variants = projectVariants(values.variants)
    const primaryMetric = buildMetric(values)
    const statisticalPlan = primaryMetric ? buildStatisticalPlan(values) : null

    if (startDate !== base.start_date) update.start_date = startDate
    if (endDate !== base.end_date) update.end_date = endDate
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

  const rules = parseTargetingRules(values.targetingRulesJson)
  if (rules.error) errors.targeting = rules.error

  const start = toAwareDateTime(values.start_date)
  const end = toAwareDateTime(values.end_date)
  if (!isAwareDateTime(start) || !isAwareDateTime(end)) {
    errors.dates = 'Use YYYY-MM-DD or an ISO 8601 timestamp with a timezone'
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
}

export function ExperimentForm({
  values,
  onChange,
  isCreate,
  currentStatus,
  onSubmit,
  submitting,
  keyError,
}: ExperimentFormProps) {
  const [errors, setErrors] = useState<ExperimentFormErrors>({})
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
    if (Object.keys(nextErrors).length > 0) return
    onSubmit()
  }

  return (
    <div className="max-w-2xl space-y-5">
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
          <Label>Flag key</Label>
          <Input
            value={values.flagKey}
            onChange={(event) => set({ flagKey: event.target.value })}
            disabled={!isCreate}
            placeholder={values.key || 'defaults to key'}
            className="font-mono text-xs"
          />
          <p className="text-xs text-muted-foreground">
            Backing flag whose exposures measure this experiment. Defaults to the key; immutable
            once created.
          </p>
          {errors.flagKey ? <p className="text-xs text-destructive">{errors.flagKey}</p> : null}
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
              : 'Running enables the backing flag; completed/stopped disable it.'}
          </p>
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

      <div className="space-y-2">
        <Label>Variants</Label>
        <div className="space-y-2">
          {values.variants.map((variant, index) => (
            <div key={index} className="flex flex-wrap items-start gap-2">
              <Input
                value={variant.key}
                onChange={(event) => setVariant(index, { key: event.target.value })}
                placeholder="key"
                aria-label={`Variant ${index + 1} key`}
                className="w-40 font-mono text-xs"
              />
              <Input
                type="number"
                min={1}
                step={1}
                value={variant.weight}
                onChange={(event) =>
                  setVariant(index, { weight: Math.max(1, Math.floor(Number(event.target.value) || 1)) })
                }
                aria-label={`Variant ${index + 1} weight`}
                className="w-24 tabular-nums"
              />
              <Input
                value={variant.description}
                onChange={(event) => setVariant(index, { description: event.target.value })}
                placeholder="description (optional)"
                aria-label={`Variant ${index + 1} description`}
                className="min-w-44 flex-1"
              />
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
          Weights set the split; traffic % gates who enters the experiment.
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Control variant</Label>
          <Select
            value={values.default_variant}
            onChange={(event) => set({ default_variant: event.target.value })}
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
          <Label>Traffic %</Label>
          <Input
            type="number"
            min={0}
            max={100}
            step="any"
            value={values.traffic_percentage}
            onChange={(event) =>
              set({ traffic_percentage: Math.min(100, Math.max(0, Number(event.target.value) || 0)) })
            }
            className="tabular-nums"
          />
        </div>
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
          <Label>Start date</Label>
          <Input
            value={values.start_date}
            onChange={(event) => set({ start_date: event.target.value })}
            placeholder="YYYY-MM-DD"
          />
        </div>
        <div className="space-y-1.5">
          <Label>End date</Label>
          <Input
            value={values.end_date}
            onChange={(event) => set({ end_date: event.target.value })}
            placeholder="YYYY-MM-DD"
          />
        </div>
      </div>
      {errors.dates ? <p className="text-xs text-destructive">{errors.dates}</p> : null}

      <div className="space-y-1.5">
        <Label>Targeting rules (JSON array)</Label>
        <Textarea
          value={values.targetingRulesJson}
          onChange={(event) => set({ targetingRulesJson: event.target.value })}
          className="min-h-24 font-mono text-xs"
          aria-label="Targeting rules JSON"
          placeholder="[]"
        />
        {errors.targeting ? <p className="text-xs text-destructive">{errors.targeting}</p> : null}
        <p className="text-xs text-muted-foreground">
          Canonical GateRule[] — each rule needs <code>id</code>, <code>conditions</code>, and a{' '}
          <code>rollout</code>. Leave empty to target everyone.
        </p>
      </div>

      <Button onClick={submit} disabled={submitting || values.key.trim() === ''}>
        {isCreate ? 'Create experiment' : 'Save changes'}
      </Button>
    </div>
  )
}
