// Editor model for preset (deterministic) tool calls: per-tool form state,
// conversion to wire `{tool, params}` entries, validation, and the reverse
// mapping for edit-mode prefill. Event selection reuses the analytics
// SelectorBuilder model so preset queries are built with the same UI as the
// Funnels/Retention pages, not raw JSON.
import type { PresetToolCall } from '@/api/types/agents'
import type { EventFilterOperator } from '@/api/types/query'
import {
  EXISTENCE_FILTER_OPERATORS,
  LIST_FILTER_OPERATORS,
  emptySelector,
  selectorProblem,
  selectorToWire,
  type FilterFormValues,
  type SelectorFormValues,
} from '@/features/analytics/selectorModel'

export const MAX_PRESET_TOOLS = 10
// The agents-side EventSelector caps filters at 10 (analytics allows 25).
export const MAX_PRESET_FILTERS = 10

export const PRESET_INTERVALS = ['1 HOUR', '1 DAY', '1 WEEK', '1 MONTH'] as const
export type PresetInterval = (typeof PRESET_INTERVALS)[number]
export const PRESET_PERIODS = ['day', 'week'] as const
export type PresetPeriod = (typeof PRESET_PERIODS)[number]

/**
 * One preset query being edited. A single flat shape covers every catalog
 * tool — each tool reads only the fields its form renders, and unused fields
 * ride along harmlessly (they are dropped at wire conversion).
 */
export interface PresetDraft {
  tool: string
  /** discover_events / query_breakdown result cap. */
  limit: number
  /** query_events selectors / query_funnel steps. */
  selectors: SelectorFormValues[]
  /** Single-selector tools: timeseries, breakdown, retention cohort, cohort metric. */
  selector: SelectorFormValues
  /** query_retention return selector. */
  returnSelector: SelectorFormValues
  interval: PresetInterval
  windowDays: number
  period: PresetPeriod
  /** cohort_property / property_name / component, depending on the tool. */
  property: string
}

function defaultLimit(tool: string): number {
  return tool === 'query_breakdown' ? 20 : 100
}

/** Minimum selector-list length a tool needs to be expressible. */
function minSelectors(tool: string): number {
  if (tool === 'query_funnel') return 2
  if (tool === 'query_events') return 1
  return 0
}

export function maxSelectors(tool: string): number {
  return tool === 'query_funnel' ? 8 : 10
}

/** Pad the selector list up to the tool's minimum (e.g. on tool switch). */
export function normalizePresetDraft(draft: PresetDraft): PresetDraft {
  const minimum = minSelectors(draft.tool)
  if (draft.selectors.length >= minimum) return draft
  const padding = Array.from({ length: minimum - draft.selectors.length }, () => emptySelector())
  return { ...draft, selectors: [...draft.selectors, ...padding] }
}

export function emptyPresetDraft(tool: string): PresetDraft {
  return normalizePresetDraft({
    tool,
    limit: defaultLimit(tool),
    selectors: [],
    selector: emptySelector(),
    returnSelector: emptySelector(),
    interval: '1 DAY',
    windowDays: 7,
    period: 'day',
    property: '',
  })
}

export function presetToWire(draft: PresetDraft): PresetToolCall {
  switch (draft.tool) {
    case 'discover_events':
      return { tool: draft.tool, params: { limit: draft.limit } }
    case 'query_events':
      return { tool: draft.tool, params: { selectors: draft.selectors.map(selectorToWire) } }
    case 'query_timeseries':
      return {
        tool: draft.tool,
        params: { selector: selectorToWire(draft.selector), interval: draft.interval },
      }
    case 'query_funnel':
      return {
        tool: draft.tool,
        params: { steps: draft.selectors.map(selectorToWire), window_days: draft.windowDays },
      }
    case 'query_retention':
      return {
        tool: draft.tool,
        params: {
          cohort_selector: selectorToWire(draft.selector),
          return_selector: selectorToWire(draft.returnSelector),
          period: draft.period,
        },
      }
    case 'query_cohort':
      return {
        tool: draft.tool,
        params: {
          cohort_property: draft.property.trim(),
          metric_selector: selectorToWire(draft.selector),
        },
      }
    case 'query_breakdown':
      return {
        tool: draft.tool,
        params: {
          selector: selectorToWire(draft.selector),
          property_name: draft.property.trim(),
          limit: draft.limit,
        },
      }
    case 'list_ui_configs': {
      const component = draft.property.trim()
      return { tool: draft.tool, params: component ? { component } : {} }
    }
    // list_flags / get_active_experiments take no parameters.
    default:
      return { tool: draft.tool, params: {} }
  }
}

function inRange(value: number, min: number, max: number): boolean {
  return Number.isInteger(value) && value >= min && value <= max
}

/** Validation problems for one preset draft; `label` prefixes every message. */
export function presetProblems(draft: PresetDraft, label: string): string[] {
  const problems: string[] = []
  const checkSelector = (selector: SelectorFormValues, name: string) => {
    if (selector.event_name.trim() === '') {
      problems.push(`${label}: ${name} — pick an event.`)
      return
    }
    const issue = selectorProblem(selector)
    if (issue) problems.push(`${label}: ${name} — ${issue}`)
    else if (selector.filters.length > MAX_PRESET_FILTERS)
      problems.push(`${label}: ${name} — at most ${MAX_PRESET_FILTERS} filters.`)
  }
  switch (draft.tool) {
    case 'discover_events':
      if (!inRange(draft.limit, 1, 500)) problems.push(`${label}: limit must be 1-500.`)
      break
    case 'query_events':
      if (!inRange(draft.selectors.length, 1, 10))
        problems.push(`${label}: between 1 and 10 event selectors.`)
      draft.selectors.forEach((selector, index) => checkSelector(selector, `selector ${index + 1}`))
      break
    case 'query_timeseries':
      checkSelector(draft.selector, 'event')
      break
    case 'query_funnel':
      if (!inRange(draft.selectors.length, 2, 8))
        problems.push(`${label}: funnels need 2-8 steps.`)
      draft.selectors.forEach((selector, index) => checkSelector(selector, `step ${index + 1}`))
      if (!inRange(draft.windowDays, 1, 90))
        problems.push(`${label}: conversion window must be 1-90 days.`)
      break
    case 'query_retention':
      checkSelector(draft.selector, 'cohort event')
      checkSelector(draft.returnSelector, 'return event')
      break
    case 'query_cohort':
      if (draft.property.trim() === '') problems.push(`${label}: cohort property is required.`)
      checkSelector(draft.selector, 'metric event')
      break
    case 'query_breakdown':
      checkSelector(draft.selector, 'event')
      if (draft.property.trim() === '') problems.push(`${label}: property name is required.`)
      if (!inRange(draft.limit, 1, 100)) problems.push(`${label}: limit must be 1-100.`)
      break
    // list_flags / get_active_experiments / list_ui_configs: nothing to check.
  }
  return problems
}

// ---------- wire → draft (edit-mode prefill) ----------

const OPERATORS: ReadonlySet<string> = new Set<EventFilterOperator>([
  'eq', 'neq', 'in', 'not_in', 'exists', 'not_exists',
  'contains', 'gt', 'gte', 'lt', 'lte',
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function filterFromWire(value: Record<string, unknown>): FilterFormValues {
  const operator = (
    typeof value.operator === 'string' && OPERATORS.has(value.operator) ? value.operator : 'eq'
  ) as EventFilterOperator
  const property = typeof value.property === 'string' ? value.property : ''
  if (LIST_FILTER_OPERATORS.has(operator)) {
    const values = Array.isArray(value.value) ? value.value.map(String) : []
    return { property, operator, value: '', values }
  }
  if (EXISTENCE_FILTER_OPERATORS.has(operator)) {
    return { property, operator, value: '', values: [] }
  }
  return { property, operator, value: value.value == null ? '' : String(value.value), values: [] }
}

function selectorFromWire(value: unknown): SelectorFormValues {
  if (!isRecord(value)) return emptySelector()
  const filters = Array.isArray(value.filters)
    ? value.filters.filter(isRecord).map(filterFromWire)
    : []
  return {
    event_name: typeof value.event_name === 'string' ? value.event_name : '',
    filters,
  }
}

function selectorsFromWire(value: unknown): SelectorFormValues[] {
  return Array.isArray(value) ? value.map(selectorFromWire) : []
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

/** Hydrate a stored preset back into form state. Unknown shapes degrade to defaults. */
export function draftFromWire(preset: PresetToolCall): PresetDraft {
  const draft = emptyPresetDraft(preset.tool)
  const params = preset.params
  switch (preset.tool) {
    case 'discover_events':
      return { ...draft, limit: numberOr(params.limit, draft.limit) }
    case 'query_events':
      return normalizePresetDraft({ ...draft, selectors: selectorsFromWire(params.selectors) })
    case 'query_timeseries':
      return {
        ...draft,
        selector: selectorFromWire(params.selector),
        interval: PRESET_INTERVALS.includes(params.interval as PresetInterval)
          ? (params.interval as PresetInterval)
          : draft.interval,
      }
    case 'query_funnel':
      return normalizePresetDraft({
        ...draft,
        selectors: selectorsFromWire(params.steps),
        windowDays: numberOr(params.window_days, draft.windowDays),
      })
    case 'query_retention':
      return {
        ...draft,
        selector: selectorFromWire(params.cohort_selector),
        returnSelector: selectorFromWire(params.return_selector),
        period: PRESET_PERIODS.includes(params.period as PresetPeriod)
          ? (params.period as PresetPeriod)
          : draft.period,
      }
    case 'query_cohort':
      return {
        ...draft,
        property: stringOr(params.cohort_property, ''),
        selector: selectorFromWire(params.metric_selector),
      }
    case 'query_breakdown':
      return {
        ...draft,
        selector: selectorFromWire(params.selector),
        property: stringOr(params.property_name, ''),
        limit: numberOr(params.limit, draft.limit),
      }
    case 'list_ui_configs':
      return { ...draft, property: stringOr(params.component, '') }
    default:
      return draft
  }
}
