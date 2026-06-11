// SelectorBuilder (plan §5.5.1): event_name + filter rows with the QUERY
// operator vocabulary (11 ops — deliberately type-distinct from flag rule
// conditions, AD-6) and operator-adaptive value inputs.
import { Plus, Trash2 } from 'lucide-react'

import type { EventFilterOperator } from '@/api/types/query'
import { TagInput } from '@/components/shared/TagInput'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'

import {
  COMMON_EVENTS,
  emptyFilter,
  EXISTENCE_FILTER_OPERATORS,
  LIST_FILTER_OPERATORS,
  NUMERIC_FILTER_OPERATORS,
  type FilterFormValues,
  type SelectorFormValues,
} from './selectorModel'

const OPERATORS: EventFilterOperator[] = [
  'eq',
  'neq',
  'in',
  'not_in',
  'exists',
  'not_exists',
  'contains',
  'gt',
  'gte',
  'lt',
  'lte',
]

interface SelectorBuilderProps {
  value: SelectorFormValues
  onChange: (next: SelectorFormValues) => void
  eventLabel?: string
}

export function SelectorBuilder({ value, onChange, eventLabel = 'Event name' }: SelectorBuilderProps) {
  const updateFilter = (index: number, patch: Partial<FilterFormValues>) => {
    onChange({
      ...value,
      filters: value.filters.map((filter, filterIndex) =>
        filterIndex === index ? { ...filter, ...patch } : filter,
      ),
    })
  }

  return (
    <div className="space-y-2">
      <datalist id="apdl-common-events">
        {COMMON_EVENTS.map((event) => (
          <option key={event} value={event} />
        ))}
      </datalist>
      <Input
        value={value.event_name}
        onChange={(event) => onChange({ ...value, event_name: event.target.value })}
        placeholder="$pageview — exact event name"
        list="apdl-common-events"
        className="font-mono text-xs"
        aria-label={eventLabel}
      />
      {value.filters.map((filter, index) => (
        <div key={index} className="flex flex-wrap items-center gap-2 pl-3">
          <span className="text-xs text-muted-foreground">where</span>
          <Input
            value={filter.property}
            onChange={(event) => updateFilter(index, { property: event.target.value })}
            placeholder="property"
            className="w-40 font-mono text-xs"
            aria-label={`Filter ${index + 1} property`}
          />
          <Select
            value={filter.operator}
            onChange={(event) =>
              updateFilter(index, { operator: event.target.value as EventFilterOperator })
            }
            className="w-32"
            aria-label={`Filter ${index + 1} operator`}
          >
            {OPERATORS.map((operator) => (
              <option key={operator} value={operator}>
                {operator}
              </option>
            ))}
          </Select>
          {EXISTENCE_FILTER_OPERATORS.has(filter.operator) ? (
            <span className="text-xs text-muted-foreground">no value</span>
          ) : LIST_FILTER_OPERATORS.has(filter.operator) ? (
            <div className="min-w-44 flex-1">
              <TagInput
                value={filter.values}
                onChange={(values) => updateFilter(index, { values })}
                placeholder="add value, press Enter"
                aria-label={`Filter ${index + 1} values`}
              />
            </div>
          ) : (
            <Input
              value={filter.value}
              onChange={(event) => updateFilter(index, { value: event.target.value })}
              type={NUMERIC_FILTER_OPERATORS.has(filter.operator) ? 'number' : 'text'}
              step="any"
              placeholder="value"
              className="min-w-36 flex-1"
              aria-label={`Filter ${index + 1} value`}
            />
          )}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() =>
              onChange({ ...value, filters: value.filters.filter((_, filterIndex) => filterIndex !== index) })
            }
            aria-label={`Remove filter ${index + 1}`}
          >
            <Trash2 />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="text-muted-foreground"
        onClick={() => onChange({ ...value, filters: [...value.filters, emptyFilter()] })}
        disabled={value.filters.length >= 25}
      >
        <Plus />
        Add filter
      </Button>
    </div>
  )
}
