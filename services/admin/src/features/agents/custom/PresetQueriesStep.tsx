// Wizard step 2: preset (deterministic) queries. Each preset fixes a catalog
// tool and its parameters at authoring time; the agent runs them verbatim on
// every run, before reasoning. Parameters are edited with structured forms —
// event pickers, filter rows, enum selects — via the analytics SelectorBuilder,
// not raw JSON; presetModel.ts converts drafts to wire params.
import { Plus, Trash2 } from 'lucide-react'

import type { AgentDefinitionsResponse } from '@/api/types/agents'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { SelectorBuilder } from '@/features/analytics/SelectorBuilder'
import { emptySelector, type SelectorFormValues } from '@/features/analytics/selectorModel'

import {
  MAX_PRESET_TOOLS,
  PRESET_INTERVALS,
  PRESET_PERIODS,
  emptyPresetDraft,
  maxSelectors,
  normalizePresetDraft,
  type PresetDraft,
  type PresetInterval,
  type PresetPeriod,
} from './presetModel'

interface PresetQueriesStepProps {
  presets: PresetDraft[]
  onChange: (next: PresetDraft[]) => void
  definitions: AgentDefinitionsResponse | undefined
}

export function PresetQueriesStep({ presets, onChange, definitions }: PresetQueriesStepProps) {
  const catalog = definitions?.tool_catalog ?? []

  const patch = (index: number, patchValue: Partial<PresetDraft>) => {
    onChange(
      presets.map((draft, draftIndex) =>
        draftIndex === index ? normalizePresetDraft({ ...draft, ...patchValue }) : draft,
      ),
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Preset queries</CardTitle>
        <CardDescription>
          Queries with parameters you fix now. They run on every run, before the agent starts
          reasoning, and their results are handed to it up front — the same baseline data every
          time. Leave open-ended discovery to the agentic data tools (next steps); this step is
          optional.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {presets.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No preset queries. The agent will start from just its prompt and gather everything
            itself.
          </p>
        ) : null}

        {presets.map((draft, index) => {
          const entry = catalog.find((tool) => tool.name === draft.tool)
          return (
            <div key={index} className="space-y-3 rounded-md border p-3">
              <div className="flex items-center gap-2">
                <div className="flex-1">
                  <Select
                    aria-label={`Preset query ${index + 1} tool`}
                    value={draft.tool}
                    onChange={(event) =>
                      // Tool switch resets the params — shapes rarely carry over.
                      onChange(
                        presets.map((prev, prevIndex) =>
                          prevIndex === index ? emptyPresetDraft(event.target.value) : prev,
                        ),
                      )
                    }
                  >
                    {catalog.map((tool) => (
                      <option key={tool.name} value={tool.name}>
                        {tool.name}
                      </option>
                    ))}
                  </Select>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Remove preset query ${index + 1}`}
                  onClick={() => onChange(presets.filter((_, prevIndex) => prevIndex !== index))}
                >
                  <Trash2 />
                </Button>
              </div>
              {entry ? <p className="text-xs text-muted-foreground">{entry.description}</p> : null}
              <PresetParamsForm draft={draft} onPatch={(value) => patch(index, value)} />
            </div>
          )
        })}

        <Button
          variant="outline"
          size="sm"
          disabled={catalog.length === 0 || presets.length >= MAX_PRESET_TOOLS}
          onClick={() =>
            onChange([...presets, emptyPresetDraft(catalog[0]?.name ?? 'discover_events')])
          }
        >
          <Plus />
          Add preset query
        </Button>
        {catalog.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Tool catalog unavailable — is the agents service reachable?
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}

function PresetParamsForm({
  draft,
  onPatch,
}: {
  draft: PresetDraft
  onPatch: (patch: Partial<PresetDraft>) => void
}) {
  switch (draft.tool) {
    case 'discover_events':
      return (
        <NumberField
          label="Max events"
          hint="How many event names to list, most frequent first (1-500)."
          min={1}
          max={500}
          value={draft.limit}
          onChange={(limit) => onPatch({ limit })}
        />
      )
    case 'query_events':
      return (
        <SelectorList
          draft={draft}
          onPatch={onPatch}
          noun="selector"
          addLabel="Add event selector"
        />
      )
    case 'query_timeseries':
      return (
        <div className="space-y-3">
          <LabeledSelector
            label="Event"
            value={draft.selector}
            onChange={(selector) => onPatch({ selector })}
          />
          <EnumField
            label="Interval"
            value={draft.interval}
            options={PRESET_INTERVALS}
            onChange={(interval) => onPatch({ interval: interval as PresetInterval })}
          />
        </div>
      )
    case 'query_funnel':
      return (
        <div className="space-y-3">
          <SelectorList draft={draft} onPatch={onPatch} noun="step" addLabel="Add step" ordered />
          <NumberField
            label="Conversion window (days)"
            hint="How long a user has to complete all steps (1-90)."
            min={1}
            max={90}
            value={draft.windowDays}
            onChange={(windowDays) => onPatch({ windowDays })}
          />
        </div>
      )
    case 'query_retention':
      return (
        <div className="space-y-3">
          <LabeledSelector
            label="Cohort event"
            hint="Actors enter on their first match in the selected dates; existing actors may re-enter."
            value={draft.selector}
            onChange={(selector) => onPatch({ selector })}
          />
          <LabeledSelector
            label="Return event"
            hint="Doing this later counts as retained."
            value={draft.returnSelector}
            onChange={(returnSelector) => onPatch({ returnSelector })}
          />
          <EnumField
            label="Period"
            value={draft.period}
            options={PRESET_PERIODS}
            onChange={(period) => onPatch({ period: period as PresetPeriod })}
          />
        </div>
      )
    case 'query_cohort':
      return (
        <div className="space-y-3">
          <TextField
            label="Cohort property"
            hint="User property that splits the cohorts, e.g. plan."
            placeholder="plan"
            value={draft.property}
            onChange={(property) => onPatch({ property })}
          />
          <LabeledSelector
            label="Metric event"
            hint="The event compared across cohorts."
            value={draft.selector}
            onChange={(selector) => onPatch({ selector })}
          />
        </div>
      )
    case 'query_breakdown':
      return (
        <div className="space-y-3">
          <LabeledSelector
            label="Event"
            value={draft.selector}
            onChange={(selector) => onPatch({ selector })}
          />
          <TextField
            label="Break down by property"
            placeholder="utm_source"
            value={draft.property}
            onChange={(property) => onPatch({ property })}
          />
          <NumberField
            label="Max values"
            hint="Top property values to return (1-100)."
            min={1}
            max={100}
            value={draft.limit}
            onChange={(limit) => onPatch({ limit })}
          />
        </div>
      )
    case 'list_ui_configs':
      return (
        <TextField
          label="Component (optional)"
          hint="Limit to one UI component; empty lists all."
          placeholder="hero_banner"
          value={draft.property}
          onChange={(property) => onPatch({ property })}
        />
      )
    // list_flags / get_active_experiments
    default:
      return <p className="text-xs text-muted-foreground">This tool takes no parameters.</p>
  }
}

/** Selector list for query_events (selectors) and query_funnel (steps). */
function SelectorList({
  draft,
  onPatch,
  noun,
  addLabel,
  ordered = false,
}: {
  draft: PresetDraft
  onPatch: (patch: Partial<PresetDraft>) => void
  noun: string
  addLabel: string
  ordered?: boolean
}) {
  const capitalized = noun.charAt(0).toUpperCase() + noun.slice(1)
  const minimum = draft.tool === 'query_funnel' ? 2 : 1
  return (
    <div className="space-y-2">
      {draft.selectors.map((selector, index) => (
        <div key={index} className="rounded-md border bg-muted/30 p-2">
          <div className="mb-1 flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              {capitalized} {index + 1}
              {ordered && index === 0 ? ' (entry)' : ''}
            </span>
            <Button
              variant="ghost"
              size="icon"
              className="h-6 w-6"
              aria-label={`Remove ${noun} ${index + 1}`}
              disabled={draft.selectors.length <= minimum}
              onClick={() =>
                onPatch({
                  selectors: draft.selectors.filter((_, prevIndex) => prevIndex !== index),
                })
              }
            >
              <Trash2 />
            </Button>
          </div>
          <SelectorBuilder
            value={selector}
            eventLabel={`${capitalized} ${index + 1} event`}
            onChange={(next: SelectorFormValues) =>
              onPatch({
                selectors: draft.selectors.map((prev, prevIndex) =>
                  prevIndex === index ? next : prev,
                ),
              })
            }
          />
        </div>
      ))}
      <Button
        variant="ghost"
        size="sm"
        className="text-muted-foreground"
        disabled={draft.selectors.length >= maxSelectors(draft.tool)}
        onClick={() => onPatch({ selectors: [...draft.selectors, emptySelector()] })}
      >
        <Plus />
        {addLabel}
      </Button>
    </div>
  )
}

function LabeledSelector({
  label,
  hint,
  value,
  onChange,
}: {
  label: string
  hint?: string
  value: SelectorFormValues
  onChange: (next: SelectorFormValues) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
      <SelectorBuilder value={value} eventLabel={`${label}`} onChange={onChange} />
    </div>
  )
}

function NumberField({
  label,
  hint,
  min,
  max,
  value,
  onChange,
}: {
  label: string
  hint?: string
  min: number
  max: number
  value: number
  onChange: (next: number) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label>
        {label}
        <Input
          type="number"
          min={min}
          max={max}
          value={value}
          className="mt-1.5 w-28 tabular-nums"
          onChange={(event) => onChange(Number(event.target.value))}
        />
      </Label>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

function TextField({
  label,
  hint,
  placeholder,
  value,
  onChange,
}: {
  label: string
  hint?: string
  placeholder?: string
  value: string
  onChange: (next: string) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label>
        {label}
        <Input
          value={value}
          placeholder={placeholder}
          className="mt-1.5 font-mono"
          onChange={(event) => onChange(event.target.value)}
        />
      </Label>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

function EnumField({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: readonly string[]
  onChange: (next: string) => void
}) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      <Select
        aria-label={label}
        value={value}
        className="w-36"
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </Select>
    </div>
  )
}
