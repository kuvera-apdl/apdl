import { Plus, X } from 'lucide-react'
import { useState, type KeyboardEvent, type ReactNode } from 'react'

import type { ConditionOperator } from '@/api/types/flags'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import {
  EQUALITY_OPERATORS,
  MAX_IDENTIFIER_LENGTH,
  MAX_MEMBERSHIP_VALUES,
  MAX_STRING_LENGTH,
  MEMBERSHIP_OPERATORS,
  NUMERIC_OPERATORS,
  PRESENCE_OPERATORS,
  parseNumeric,
  scalarEqual,
  type JsonScalar,
} from '@/core/evaluator/targetingContract'

import {
  COMMON_TARGETING_ATTRIBUTES,
  TARGETING_ATTRIBUTES_DATALIST_ID,
  TARGETING_OPERATOR_GROUPS,
  targetingConditionForOperator,
  type ScalarValueType,
  type TargetingConditionFormValue,
} from './editorModel'

export function TargetingAttributesDatalist() {
  return (
    <datalist id={TARGETING_ATTRIBUTES_DATALIST_ID}>
      {COMMON_TARGETING_ATTRIBUTES.map((attribute) => (
        <option key={attribute} value={attribute} />
      ))}
    </datalist>
  )
}

interface ScalarListInputProps {
  id: string
  value: JsonScalar[]
  valueType: ScalarValueType
  onValueTypeChange: (next: ScalarValueType) => void
  onChange: (next: JsonScalar[]) => void
  ariaLabel: string
}

function ScalarListInput({
  id,
  value,
  valueType,
  onValueTypeChange,
  onChange,
  ariaLabel,
}: ScalarListInputProps) {
  const [draft, setDraft] = useState('')
  const [booleanDraft, setBooleanDraft] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const commit = () => {
    if (value.length >= MAX_MEMBERSHIP_VALUES) {
      setError(`At most ${MAX_MEMBERSHIP_VALUES} values`)
      return
    }

    let nextValue: JsonScalar
    if (valueType === 'boolean') {
      nextValue = booleanDraft
    } else if (valueType === 'number') {
      const numeric = parseNumeric(draft.trim())
      if (numeric === null) {
        if (draft.trim() !== '') setError('Enter a finite number or canonical decimal')
        return
      }
      nextValue = numeric
    } else {
      const text = draft.trim()
      if (text === '') return
      if (text.length > MAX_STRING_LENGTH) {
        setError(`Use at most ${MAX_STRING_LENGTH} characters`)
        return
      }
      nextValue = text
    }

    if (!value.some((current) => scalarEqual(current, nextValue))) {
      onChange([...value, nextValue])
    }
    setDraft('')
    setError(null)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' || event.key === ',') {
      event.preventDefault()
      commit()
    } else if (event.key === 'Backspace' && draft === '' && value.length > 0) {
      onChange(value.slice(0, -1))
    }
  }

  return (
    <div className="space-y-1">
      <div className="flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-md border border-input bg-transparent px-2 py-1 text-sm shadow-sm focus-within:ring-1 focus-within:ring-ring">
        {value.map((entry, index) => (
          <span
            key={`${typeof entry}:${String(entry)}:${index}`}
            className="inline-flex items-center gap-1 rounded-full border bg-secondary px-2 py-0.5 text-xs"
          >
            <span className="text-[10px] uppercase text-muted-foreground">{typeof entry}</span>
            {String(entry)}
            <button
              type="button"
              onClick={() => onChange(value.filter((_, valueIndex) => valueIndex !== index))}
              aria-label={`Remove ${typeof entry} value ${String(entry)}`}
              className="text-muted-foreground hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <Select
          value={valueType}
          onChange={(event) => {
            setDraft('')
            setError(null)
            onValueTypeChange(event.target.value as ScalarValueType)
          }}
          aria-label={`${ariaLabel} type`}
          className="h-7 w-24 border-0 px-1 shadow-none"
        >
          <option value="string">String</option>
          <option value="number">Number</option>
          <option value="boolean">Boolean</option>
        </Select>
        {valueType === 'boolean' ? (
          <Select
            id={id}
            value={String(booleanDraft)}
            onChange={(event) => setBooleanDraft(event.target.value === 'true')}
            aria-label={ariaLabel}
            className="h-7 min-w-20 flex-1 border-0 shadow-none"
          >
            <option value="false">false</option>
            <option value="true">true</option>
          </Select>
        ) : (
          <input
            id={id}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value)
              setError(null)
            }}
            onKeyDown={onKeyDown}
            placeholder={valueType === 'number' ? 'canonical decimal' : 'add value, press Enter'}
            aria-label={ariaLabel}
            maxLength={MAX_STRING_LENGTH}
            className="min-w-24 flex-1 bg-transparent outline-none placeholder:text-muted-foreground"
          />
        )}
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={commit}
          disabled={value.length >= MAX_MEMBERSHIP_VALUES}
          aria-label={`Add ${ariaLabel.toLowerCase()}`}
          className="h-7 w-7"
        >
          <Plus />
        </Button>
      </div>
      <p className="text-[11px] text-muted-foreground">
        {value.length}/{MAX_MEMBERSHIP_VALUES} typed values
      </p>
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
    </div>
  )
}

export interface TargetingConditionAriaLabels {
  attribute: string
  operator: string
  value: string
  valueType: string
  values: string
}

export interface TargetingConditionFieldErrors {
  attribute?: string
  value?: string
  values?: string
}

interface TargetingConditionFieldsProps {
  condition: TargetingConditionFormValue
  onChange: (next: TargetingConditionFormValue) => void
  idPrefix: string
  ariaLabels: TargetingConditionAriaLabels
  attributeLabel?: string
  showLabels?: boolean
  errors?: TargetingConditionFieldErrors
}

function FieldLabel({
  htmlFor,
  show,
  children,
}: {
  htmlFor?: string
  show: boolean
  children: ReactNode
}) {
  return show ? <Label htmlFor={htmlFor}>{children}</Label> : null
}

/** Shared controlled fields for flag and experiment targeting conditions. */
export function TargetingConditionFields({
  condition,
  onChange,
  idPrefix,
  ariaLabels,
  attributeLabel = 'Attribute',
  showLabels = false,
  errors,
}: TargetingConditionFieldsProps) {
  const attributeId = `${idPrefix}-attribute`
  const operatorId = `${idPrefix}-operator`
  const valueId = `${idPrefix}-value`

  return (
    <div className="contents">
      <div className="space-y-1.5">
        <FieldLabel htmlFor={attributeId} show={showLabels}>
          {attributeLabel}
        </FieldLabel>
        <Input
          id={attributeId}
          value={condition.attribute}
          onChange={(event) => onChange({ ...condition, attribute: event.target.value })}
          list={TARGETING_ATTRIBUTES_DATALIST_ID}
          placeholder={attributeLabel === 'Type' ? 'Select or enter a type' : 'attribute'}
          aria-label={ariaLabels.attribute}
          className="font-mono text-xs"
          maxLength={MAX_IDENTIFIER_LENGTH}
        />
        {errors?.attribute ? <p className="text-xs text-destructive">{errors.attribute}</p> : null}
      </div>
      <div className="space-y-1.5">
        <FieldLabel htmlFor={operatorId} show={showLabels}>
          Operator
        </FieldLabel>
        <Select
          id={operatorId}
          value={condition.operator}
          onChange={(event) =>
            onChange(
              targetingConditionForOperator(condition, event.target.value as ConditionOperator),
            )
          }
          aria-label={ariaLabels.operator}
        >
          {TARGETING_OPERATOR_GROUPS.map((group) => (
            <optgroup key={group.label} label={group.label}>
              {group.operators.map((operator) => (
                <option key={operator} value={operator}>
                  {operator.replace(/_/g, ' ')}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
      </div>
      <div className="space-y-1.5">
        <FieldLabel
          htmlFor={PRESENCE_OPERATORS.has(condition.operator) ? undefined : valueId}
          show={showLabels}
        >
          Value
        </FieldLabel>
        {PRESENCE_OPERATORS.has(condition.operator) ? (
          <p className="py-2 text-xs text-muted-foreground">No value — checks presence</p>
        ) : MEMBERSHIP_OPERATORS.has(condition.operator) ? (
          <>
            <ScalarListInput
              id={valueId}
              value={condition.values}
              valueType={condition.valueType}
              onValueTypeChange={(valueType) => onChange({ ...condition, valueType })}
              onChange={(values) => onChange({ ...condition, values })}
              ariaLabel={ariaLabels.values}
            />
            {errors?.values ? <p className="text-xs text-destructive">{errors.values}</p> : null}
          </>
        ) : (
          <>
            <div className="flex gap-2">
              {EQUALITY_OPERATORS.has(condition.operator) ? (
                <Select
                  value={condition.valueType}
                  onChange={(event) => {
                    const valueType = event.target.value as ScalarValueType
                    onChange({
                      ...condition,
                      valueType,
                      value:
                        valueType === 'boolean'
                          ? false
                          : valueType === 'number'
                            ? ''
                            : String(condition.value),
                    })
                  }}
                  aria-label={ariaLabels.valueType}
                  className="w-28"
                >
                  <option value="string">String</option>
                  <option value="number">Number</option>
                  <option value="boolean">Boolean</option>
                </Select>
              ) : null}
              {condition.valueType === 'boolean' &&
              EQUALITY_OPERATORS.has(condition.operator) ? (
                <Select
                  id={valueId}
                  value={String(condition.value)}
                  onChange={(event) =>
                    onChange({ ...condition, value: event.target.value === 'true' })
                  }
                  aria-label={ariaLabels.value}
                  className="flex-1"
                >
                  <option value="false">false</option>
                  <option value="true">true</option>
                </Select>
              ) : (
                <Input
                  id={valueId}
                  type="text"
                  value={String(condition.value)}
                  onChange={(event) => onChange({ ...condition, value: event.target.value })}
                  placeholder={
                    NUMERIC_OPERATORS.has(condition.operator) || condition.valueType === 'number'
                      ? 'canonical decimal'
                      : 'value'
                  }
                  aria-label={ariaLabels.value}
                  maxLength={MAX_STRING_LENGTH}
                  className="min-w-0 flex-1 font-mono text-xs"
                />
              )}
            </div>
            {errors?.value ? <p className="text-xs text-destructive">{errors.value}</p> : null}
          </>
        )}
      </div>
    </div>
  )
}
