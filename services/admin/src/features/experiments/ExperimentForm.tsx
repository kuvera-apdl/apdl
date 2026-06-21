// Experiment setup form (gap G5): a structured editor over the canonical record.
// An experiment owns a backing flag, so variants/default_variant/traffic map to
// the flag and status drives flag serving through lifecycle-aware transitions.
// Targeting stays a JSON editor but is validated against the canonical GateRule
// schema rather than left raw.
import { Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { z } from 'zod'

import { gateRuleSchema } from '@/api/schemas/flags'
import type { GateRule } from '@/api/types/flags'
import type {
  ExperimentCreate,
  ExperimentEntry,
  ExperimentMetric,
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
// terminal (no resume). Create starts as draft or running.
const CREATE_STATUSES: ExperimentStatus[] = ['draft', 'running']
const STATUS_TRANSITIONS: Record<ExperimentStatus, ExperimentStatus[]> = {
  draft: ['draft', 'running', 'stopped'],
  running: ['running', 'completed', 'stopped'],
  completed: ['completed'],
  stopped: ['stopped'],
}

const METRIC_TYPES = ['conversion', 'count', 'revenue', 'duration']
const METRIC_DIRECTIONS = ['increase', 'decrease']

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
  metricType: string
  metricDirection: string
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
    metricType: 'conversion',
    metricDirection: 'increase',
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
    start_date: entry.start_date,
    end_date: entry.end_date,
    variants: entry.variants.map((variant) => ({
      key: variant.key,
      weight: variant.weight,
      description: variant.description ?? '',
    })),
    default_variant: entry.default_variant,
    metricEvent: entry.primary_metric?.event ?? '',
    metricType: entry.primary_metric?.type ?? 'conversion',
    metricDirection: entry.primary_metric?.direction ?? 'increase',
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
    type: values.metricType,
    direction: values.metricDirection,
  }
}

export function buildCreate(values: ExperimentFormValues): ExperimentCreate {
  const create: ExperimentCreate = {
    key: values.key.trim(),
    flag_key: values.flagKey.trim() || values.key.trim(),
    status: values.status,
    description: values.description,
    traffic_percentage: values.traffic_percentage,
    start_date: values.start_date,
    end_date: values.end_date,
    variants: projectVariants(values.variants),
    default_variant: values.default_variant,
    targeting_rules: parseTargetingRules(values.targetingRulesJson).value ?? [],
  }
  const metric = buildMetric(values)
  if (metric) create.primary_metric = metric
  return create
}

export function buildUpdate(values: ExperimentFormValues): ExperimentUpdate {
  const update: ExperimentUpdate = {
    status: values.status,
    description: values.description,
    traffic_percentage: values.traffic_percentage,
    start_date: values.start_date,
    end_date: values.end_date,
    variants: projectVariants(values.variants),
    default_variant: values.default_variant,
    targeting_rules: parseTargetingRules(values.targetingRulesJson).value ?? [],
  }
  const metric = buildMetric(values)
  if (metric) update.primary_metric = metric
  return update
}

interface ExperimentFormErrors {
  variants?: string
  default_variant?: string
  targeting?: string
}

export function validateExperimentForm(values: ExperimentFormValues): ExperimentFormErrors {
  const errors: ExperimentFormErrors = {}
  const keys = values.variants.map((variant) => variant.key.trim())
  if (values.variants.length === 0) errors.variants = 'Add at least one variant'
  else if (keys.some((key) => key === '')) errors.variants = 'Every variant needs a key'
  else if (new Set(keys).size !== keys.length) errors.variants = 'Variant keys must be unique'
  else if (values.variants.reduce((sum, variant) => sum + variant.weight, 0) <= 0)
    errors.variants = 'Total weight must be positive'

  if (!keys.includes(values.default_variant)) {
    errors.default_variant = 'Choose a default variant that matches a variant key'
  }

  const rules = parseTargetingRules(values.targetingRulesJson)
  if (rules.error) errors.targeting = rules.error
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
          {keyError ? <p className="text-xs text-destructive">{keyError}</p> : null}
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
                min={0}
                step={1}
                value={variant.weight}
                onChange={(event) =>
                  setVariant(index, { weight: Math.max(0, Math.floor(Number(event.target.value) || 0)) })
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
                disabled={values.variants.length <= 1}
              >
                <Trash2 />
              </Button>
            </div>
          ))}
        </div>
        <Button type="button" variant="outline" size="sm" onClick={addVariant}>
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
          <Label>Default variant</Label>
          <Select
            value={values.default_variant}
            onChange={(event) => set({ default_variant: event.target.value })}
            aria-label="Default variant"
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
            <p className="text-xs text-muted-foreground">Served when the flag is off or invalid.</p>
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
            className="font-mono text-xs"
          />
          <Select
            value={values.metricType}
            onChange={(event) => set({ metricType: event.target.value })}
            aria-label="Metric type"
          >
            {METRIC_TYPES.map((type) => (
              <option key={type} value={type}>
                {type}
              </option>
            ))}
          </Select>
          <Select
            value={values.metricDirection}
            onChange={(event) => set({ metricDirection: event.target.value })}
            aria-label="Metric direction"
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
