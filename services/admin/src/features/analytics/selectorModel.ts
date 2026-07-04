// Editor model for event selectors (plan §5.5.1 SelectorBuilder) and the
// conversion to wire EventSelector payloads. Same value-key rule as flag
// conditions: exists/not_exists omit `value` entirely.
import { eventSelectorSchema } from '@/api/schemas/query'
import type { EventFilterOperator, EventPropertyFilter, EventSelector } from '@/api/types/query'

export interface FilterFormValues {
  property: string
  operator: EventFilterOperator
  /** Raw text for scalar operators. */
  value: string
  /** Chip list for in / not_in. */
  values: string[]
}

export interface SelectorFormValues {
  event_name: string
  filters: FilterFormValues[]
}

export const EXISTENCE_FILTER_OPERATORS: ReadonlySet<EventFilterOperator> = new Set([
  'exists',
  'not_exists',
])
export const LIST_FILTER_OPERATORS: ReadonlySet<EventFilterOperator> = new Set(['in', 'not_in'])
export const NUMERIC_FILTER_OPERATORS: ReadonlySet<EventFilterOperator> = new Set([
  'gt',
  'gte',
  'lt',
  'lte',
])

export const COMMON_EVENTS = [
  'page',
  '$click',
  '$feature_flag_exposure',
  '$frontend_error',
  '$web_vital',
  'apdl_console_verification',
]

export function emptySelector(eventName = ''): SelectorFormValues {
  return { event_name: eventName, filters: [] }
}

export function emptyFilter(): FilterFormValues {
  return { property: '', operator: 'eq', value: '', values: [] }
}

export function filterToWire(filter: FilterFormValues): EventPropertyFilter {
  const property = filter.property.trim()
  if (EXISTENCE_FILTER_OPERATORS.has(filter.operator)) {
    return { property, operator: filter.operator }
  }
  if (LIST_FILTER_OPERATORS.has(filter.operator)) {
    return { property, operator: filter.operator, value: filter.values }
  }
  if (NUMERIC_FILTER_OPERATORS.has(filter.operator)) {
    return { property, operator: filter.operator, value: Number(filter.value) }
  }
  return { property, operator: filter.operator, value: filter.value }
}

export function selectorToWire(selector: SelectorFormValues): EventSelector {
  return {
    event_name: selector.event_name.trim(),
    filters: selector.filters.map(filterToWire),
  }
}

/** First validation problem of the wire form, or null when valid. */
export function selectorProblem(selector: SelectorFormValues): string | null {
  const parsed = eventSelectorSchema.safeParse(selectorToWire(selector))
  if (parsed.success) return null
  const issue = parsed.error.issues[0]
  if (!issue) return 'Invalid selector'
  const where = issue.path.length > 0 ? `${issue.path.join('.')}: ` : ''
  return `${where}${issue.message}`
}

export function selectorSummary(selector: SelectorFormValues): string {
  const name = selector.event_name.trim() || '(event)'
  if (selector.filters.length === 0) return name
  return `${name} · ${selector.filters.length} filter${selector.filters.length === 1 ? '' : 's'}`
}

/** Today in local time as YYYY-MM-DD. */
export function todayIso(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

export interface DateRange {
  start_date: string
  end_date: string
}

/** Last N days, inclusive of today. */
export function lastDays(days: number): DateRange {
  const end = new Date()
  const start = new Date()
  start.setDate(start.getDate() - (days - 1))
  const iso = (date: Date) =>
    `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  return { start_date: iso(start), end_date: iso(end) }
}

const pad2 = (value: number): string => String(value).padStart(2, '0')

/** Today in UTC as YYYY-MM-DD — the timezone the analytics pipeline buckets in. */
export function todayUtcIso(): string {
  const now = new Date()
  return `${now.getUTCFullYear()}-${pad2(now.getUTCMonth() + 1)}-${pad2(now.getUTCDate())}`
}

/**
 * The UTC calendar date(s) the last `hours` hours fall on, as an inclusive range.
 * The query API filters by UTC `event_date`, so this is the window to fetch before
 * slicing to exact hourly bins client-side (see rollingHourBuckets). Spans two
 * dates whenever the window crosses UTC midnight.
 */
export function utcDateRangeForLastHours(hours: number): DateRange {
  const now = new Date()
  const endMs = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), now.getUTCHours())
  const startMs = endMs - (hours - 1) * 3_600_000
  const isoUtc = (ms: number) => {
    const date = new Date(ms)
    return `${date.getUTCFullYear()}-${pad2(date.getUTCMonth() + 1)}-${pad2(date.getUTCDate())}`
  }
  return { start_date: isoUtc(startMs), end_date: isoUtc(endMs) }
}
