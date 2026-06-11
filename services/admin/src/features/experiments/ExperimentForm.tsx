// Experiment setup form (plan §5.4.2): the record is loose strings —
// variants/targeting_rules are schema-checked JSON editors, not structured
// builders, until gap G5 canonicalizes the model. No optimistic locking:
// last write wins, so edits re-check updated_at before submitting.
import { useState } from 'react'

import type { ExperimentCreate, ExperimentEntry } from '@/api/types/experiments'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'

import { KNOWN_EXPERIMENT_STATUSES } from './StatusPill'

export interface ExperimentFormValues {
  key: string
  status: string
  description: string
  traffic_percentage: number
  start_date: string
  end_date: string
  variantsJson: string
  targetingRulesJson: string
}

export function entryToFormValues(entry: ExperimentEntry): ExperimentFormValues {
  return {
    key: entry.key,
    status: entry.status,
    description: entry.description,
    traffic_percentage: entry.traffic_percentage,
    start_date: entry.start_date,
    end_date: entry.end_date,
    variantsJson: JSON.stringify(entry.variants ?? [], null, 2),
    // The list API does not return targeting_rules — empty means "leave
    // unchanged" on save (see buildUpdate).
    targetingRulesJson: '',
  }
}

export function emptyExperimentValues(): ExperimentFormValues {
  return {
    key: '',
    status: 'draft',
    description: '',
    traffic_percentage: 100,
    start_date: '',
    end_date: '',
    variantsJson: JSON.stringify(
      [
        { key: 'control', weight: 1 },
        { key: 'treatment', weight: 1 },
      ],
      null,
      2,
    ),
    targetingRulesJson: '[]',
  }
}

export function parseJsonArray(raw: string): { value: unknown[] | null; error: string | null } {
  if (raw.trim() === '') return { value: null, error: null }
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return { value: null, error: 'Must be a JSON array' }
    return { value: parsed, error: null }
  } catch {
    return { value: null, error: 'Invalid JSON' }
  }
}

export interface ExperimentFormProps {
  values: ExperimentFormValues
  onChange: (next: ExperimentFormValues) => void
  isCreate: boolean
  onSubmit: () => void
  submitting: boolean
  keyError?: string | null
}

export function buildCreate(values: ExperimentFormValues): ExperimentCreate {
  return {
    key: values.key.trim(),
    status: values.status.trim() || 'draft',
    description: values.description,
    traffic_percentage: values.traffic_percentage,
    start_date: values.start_date,
    end_date: values.end_date,
    variants: parseJsonArray(values.variantsJson).value ?? [],
    targeting_rules: parseJsonArray(values.targetingRulesJson).value ?? [],
  }
}

export function ExperimentForm({
  values,
  onChange,
  isCreate,
  onSubmit,
  submitting,
  keyError,
}: ExperimentFormProps) {
  const [variantsError, setVariantsError] = useState<string | null>(null)
  const [rulesError, setRulesError] = useState<string | null>(null)

  const set = (patch: Partial<ExperimentFormValues>) => onChange({ ...values, ...patch })

  const submit = () => {
    const variants = parseJsonArray(values.variantsJson)
    const rules = parseJsonArray(values.targetingRulesJson)
    setVariantsError(variants.error)
    setRulesError(rules.error)
    if (variants.error || rules.error) return
    onSubmit()
  }

  return (
    <div className="max-w-2xl space-y-4">
      <datalist id="apdl-experiment-statuses">
        {KNOWN_EXPERIMENT_STATUSES.map((status) => (
          <option key={status} value={status} />
        ))}
      </datalist>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <Label>Key</Label>
          <Input
            value={values.key}
            onChange={(event) => set({ key: event.target.value })}
            disabled={!isCreate}
            placeholder="checkout-cta-test"
            className="font-mono text-xs"
          />
          {keyError ? <p className="text-xs text-destructive">{keyError}</p> : null}
        </div>
        <div className="space-y-1.5">
          <Label>Status</Label>
          <Input
            value={values.status}
            onChange={(event) => set({ status: event.target.value })}
            list="apdl-experiment-statuses"
            placeholder="draft"
          />
          <p className="text-xs text-muted-foreground">
            Free-form string (the API does not enforce a lifecycle yet).
          </p>
        </div>
      </div>
      <div className="space-y-1.5">
        <Label>Description</Label>
        <Input
          value={values.description}
          onChange={(event) => set({ description: event.target.value })}
          placeholder="What this experiment tests"
        />
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
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
      <p className="text-xs text-muted-foreground">
        Dates are stored verbatim as strings — the API does not validate them.
      </p>
      <div className="space-y-1.5">
        <Label>Variants (JSON array)</Label>
        <Textarea
          value={values.variantsJson}
          onChange={(event) => set({ variantsJson: event.target.value })}
          className="min-h-32 font-mono text-xs"
          aria-label="Variants JSON"
        />
        {variantsError ? <p className="text-xs text-destructive">{variantsError}</p> : null}
        <p className="text-xs text-muted-foreground">
          Advisory shape: {`[{ "key", "weight", "description?" }]`} — what the experiment-design
          agent emits. The server stores any array.
        </p>
      </div>
      <div className="space-y-1.5">
        <Label>Targeting rules (JSON array)</Label>
        <Textarea
          value={values.targetingRulesJson}
          onChange={(event) => set({ targetingRulesJson: event.target.value })}
          className="min-h-24 font-mono text-xs"
          aria-label="Targeting rules JSON"
        />
        {rulesError ? <p className="text-xs text-destructive">{rulesError}</p> : null}
        {!isCreate ? (
          <p className="text-xs text-muted-foreground">
            The API does not return existing targeting rules — leave empty to keep them unchanged;
            providing an array overwrites them.
          </p>
        ) : null}
      </div>
      <Button onClick={submit} disabled={submitting || values.key.trim() === ''}>
        {isCreate ? 'Create experiment' : 'Save changes'}
      </Button>
    </div>
  )
}
