import { ArrowDown, ArrowUp, Plus, Trash2, X } from 'lucide-react'
import { useState, type KeyboardEvent } from 'react'

import type { ConditionOperator, GateCondition } from '@/api/types/flags'
import type { ExperimentTargetingRule } from '@/api/types/experiments'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import {
  EQUALITY_OPERATORS,
  MAX_CONDITIONS_PER_RULE,
  MAX_IDENTIFIER_LENGTH,
  MAX_MEMBERSHIP_VALUES,
  MAX_RULES,
  MAX_STRING_LENGTH,
  MEMBERSHIP_OPERATORS,
  NUMERIC_OPERATORS,
  PRESENCE_OPERATORS,
  isScalar,
  parseNumeric,
  scalarEqual,
  type JsonScalar,
} from '@/core/evaluator/targetingContract'

const OPERATOR_GROUPS: { label: string; operators: ConditionOperator[] }[] = [
  { label: 'Existence', operators: ['exists', 'not_exists'] },
  { label: 'Equality', operators: ['equals', 'not_equals'] },
  { label: 'String', operators: ['contains', 'not_contains', 'starts_with', 'ends_with'] },
  { label: 'Numeric', operators: ['gt', 'gte', 'lt', 'lte'] },
  { label: 'Collection', operators: ['in', 'not_in'] },
]

const COMMON_TYPES = [
  'user_id',
  'anonymous_id',
  'plan',
  'country',
  'email',
  'device',
  'beta_opt_in',
]

export interface ExperimentTargetingConditionFormValue {
  attribute: string
  operator: ConditionOperator
  valueType: ScalarValueType
  value: JsonScalar
  values: JsonScalar[]
}

export type ScalarValueType = 'string' | 'number' | 'boolean'

export interface ExperimentTargetingRuleFormValue {
  id: string
  name: string
  conditions: ExperimentTargetingConditionFormValue[]
}

function scalarValueType(value: JsonScalar): ScalarValueType {
  if (typeof value === 'number') return 'number'
  if (typeof value === 'boolean') return 'boolean'
  return 'string'
}

function conditionToFormValue(condition: GateCondition): ExperimentTargetingConditionFormValue {
  if (PRESENCE_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute,
      operator: condition.operator,
      valueType: 'string',
      value: '',
      values: [],
    }
  }
  if (MEMBERSHIP_OPERATORS.has(condition.operator)) {
    return {
      attribute: condition.attribute,
      operator: condition.operator,
      valueType:
        Array.isArray(condition.value) && isScalar(condition.value[0])
          ? scalarValueType(condition.value[0])
          : 'string',
      value: '',
      values: Array.isArray(condition.value) ? condition.value.filter(isScalar) : [],
    }
  }
  const value = isScalar(condition.value) ? condition.value : ''
  return {
    attribute: condition.attribute,
    operator: condition.operator,
    valueType: NUMERIC_OPERATORS.has(condition.operator)
      ? 'number'
      : scalarValueType(value),
    value,
    values: [],
  }
}

function conditionToWire(
  condition: ExperimentTargetingConditionFormValue,
): GateCondition {
  const base = {
    attribute: condition.attribute.trim(),
    operator: condition.operator,
  }
  if (PRESENCE_OPERATORS.has(condition.operator)) return base
  if (MEMBERSHIP_OPERATORS.has(condition.operator)) {
    return { ...base, value: [...condition.values] }
  }
  if (NUMERIC_OPERATORS.has(condition.operator) || condition.valueType === 'number') {
    return { ...base, value: parseNumeric(condition.value) ?? Number.NaN }
  }
  if (condition.valueType === 'boolean') {
    return {
      ...base,
      value: typeof condition.value === 'boolean' ? condition.value : null,
    }
  }
  return {
    ...base,
    value:
      typeof condition.value === 'string' ? condition.value : String(condition.value),
  }
}

export function targetingRulesToFormValues(
  rules: ExperimentTargetingRule[],
): ExperimentTargetingRuleFormValue[] {
  return rules.map((rule) => ({
    id: rule.id,
    name: rule.name,
    conditions: rule.conditions.map(conditionToFormValue),
  }))
}

export function targetingRulesToWire(
  rules: ExperimentTargetingRuleFormValue[],
): ExperimentTargetingRule[] {
  return rules.map((rule) => ({
    id: rule.id,
    name: rule.name,
    conditions: rule.conditions.map(conditionToWire),
  }))
}

function newRuleId(): string {
  return `rule_${crypto.randomUUID().replace(/-/g, '').slice(0, 12)}`
}

function emptyCondition(): ExperimentTargetingConditionFormValue {
  return {
    attribute: '',
    operator: 'equals',
    valueType: 'string',
    value: '',
    values: [],
  }
}

function emptyRule(): ExperimentTargetingRuleFormValue {
  return {
    id: newRuleId(),
    name: '',
    conditions: [emptyCondition()],
  }
}

function conditionForOperator(
  condition: ExperimentTargetingConditionFormValue,
  operator: ConditionOperator,
): ExperimentTargetingConditionFormValue {
  if (PRESENCE_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'string',
      value: '',
      values: [],
    }
  }
  if (MEMBERSHIP_OPERATORS.has(operator)) {
    return { ...condition, operator, value: '', values: condition.values }
  }
  if (NUMERIC_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'number',
      value: parseNumeric(condition.value) === null ? '' : condition.value,
      values: [],
    }
  }
  if (!EQUALITY_OPERATORS.has(operator)) {
    return {
      ...condition,
      operator,
      valueType: 'string',
      value: typeof condition.value === 'string' ? condition.value : '',
      values: [],
    }
  }
  return {
    ...condition,
    operator,
    value:
      MEMBERSHIP_OPERATORS.has(condition.operator) &&
      condition.valueType === 'boolean'
        ? false
        : condition.value,
    values: [],
  }
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
            <span className="text-[10px] uppercase text-muted-foreground">
              {typeof entry}
            </span>
            {String(entry)}
            <button
              type="button"
              onClick={() =>
                onChange(value.filter((_, valueIndex) => valueIndex !== index))
              }
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
            placeholder={
              valueType === 'number'
                ? 'Canonical decimal'
                : 'Add value, press Enter'
            }
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

interface ConditionRowProps {
  ruleIndex: number
  conditionIndex: number
  condition: ExperimentTargetingConditionFormValue
  onChange: (next: ExperimentTargetingConditionFormValue) => void
  onAddAfter: () => void
  onRemove: () => void
  canAdd: boolean
}

function ConditionRow({
  ruleIndex,
  conditionIndex,
  condition,
  onChange,
  onAddAfter,
  onRemove,
  canAdd,
}: ConditionRowProps) {
  const prefix = `Targeting rule ${ruleIndex + 1} condition ${conditionIndex + 1}`
  const typeId = `targeting-rule-${ruleIndex}-condition-${conditionIndex}-type`
  const operatorId = `targeting-rule-${ruleIndex}-condition-${conditionIndex}-operator`
  const valueId = `targeting-rule-${ruleIndex}-condition-${conditionIndex}-value`

  return (
    <div className="grid gap-2 rounded-md bg-muted/40 p-3 sm:grid-cols-[minmax(0,1fr)_10rem_minmax(0,1fr)_auto]">
      <div className="space-y-1.5">
        <Label htmlFor={typeId}>Type</Label>
        <Input
          id={typeId}
          value={condition.attribute}
          onChange={(event) => onChange({ ...condition, attribute: event.target.value })}
          list="apdl-experiment-targeting-types"
          placeholder="Select or enter a type"
          aria-label={`${prefix} type`}
          className="font-mono text-xs"
          maxLength={MAX_IDENTIFIER_LENGTH}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor={operatorId}>Operator</Label>
        <Select
          id={operatorId}
          value={condition.operator}
          onChange={(event) =>
            onChange(conditionForOperator(condition, event.target.value as ConditionOperator))
          }
          aria-label={`${prefix} operator`}
        >
          {OPERATOR_GROUPS.map((group) => (
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
        <Label
          htmlFor={
            PRESENCE_OPERATORS.has(condition.operator) ? undefined : valueId
          }
        >
          Value
        </Label>
        {PRESENCE_OPERATORS.has(condition.operator) ? (
          <p className="py-2 text-xs text-muted-foreground">
            No value — checks presence
          </p>
        ) : MEMBERSHIP_OPERATORS.has(condition.operator) ? (
          <ScalarListInput
            id={valueId}
            value={condition.values}
            valueType={condition.valueType}
            onValueTypeChange={(valueType) =>
              onChange({
                ...condition,
                valueType,
              })
            }
            onChange={(values) => onChange({ ...condition, values })}
            ariaLabel={`${prefix} values`}
          />
        ) : (
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
                aria-label={`${prefix} value type`}
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
                  onChange({
                    ...condition,
                    value: event.target.value === 'true',
                  })
                }
                aria-label={`${prefix} value`}
                className="flex-1"
              >
                <option value="false">false</option>
                <option value="true">true</option>
              </Select>
            ) : (
              <Input
                id={valueId}
                value={String(condition.value)}
                onChange={(event) =>
                  onChange({ ...condition, value: event.target.value })
                }
                placeholder={
                  NUMERIC_OPERATORS.has(condition.operator) ||
                  condition.valueType === 'number'
                    ? 'Canonical decimal'
                    : 'Value'
                }
                aria-label={`${prefix} value`}
                maxLength={MAX_STRING_LENGTH}
                className="min-w-0 flex-1 font-mono text-xs"
              />
            )}
          </div>
        )}
      </div>
      <div className="flex items-end gap-1">
        <Button
          type="button"
          variant="outline"
          size="icon"
          onClick={onAddAfter}
          disabled={!canAdd}
          aria-label={`Add condition after targeting rule ${ruleIndex + 1} condition ${conditionIndex + 1}`}
        >
          <Plus />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onRemove}
          aria-label={`Remove targeting rule ${ruleIndex + 1} condition ${conditionIndex + 1}`}
        >
          <Trash2 />
        </Button>
      </div>
    </div>
  )
}

interface ExperimentTargetingRulesProps {
  value: ExperimentTargetingRuleFormValue[]
  onChange: (next: ExperimentTargetingRuleFormValue[]) => void
  disabled?: boolean
}

export function ExperimentTargetingRules({
  value,
  onChange,
  disabled = false,
}: ExperimentTargetingRulesProps) {
  const updateRule = (index: number, next: ExperimentTargetingRuleFormValue) => {
    onChange(value.map((rule, ruleIndex) => (ruleIndex === index ? next : rule)))
  }

  const moveRule = (from: number, to: number) => {
    const next = [...value]
    const [rule] = next.splice(from, 1)
    if (!rule) return
    next.splice(to, 0, rule)
    onChange(next)
  }

  return (
    <fieldset className="space-y-3" disabled={disabled}>
      <legend className="text-sm font-medium">Targeting rules</legend>
      <datalist id="apdl-experiment-targeting-types">
        {COMMON_TYPES.map((type) => (
          <option key={type} value={type} />
        ))}
      </datalist>
      {value.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No targeting rules — everyone is eligible for the traffic allocation.
        </p>
      ) : (
        value.map((rule, ruleIndex) => (
          <div key={`${rule.id}:${ruleIndex}`} className="space-y-3 rounded-md border p-4">
            <div className="flex items-center gap-2">
              <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
                {ruleIndex + 1}
              </span>
              <Input
                value={rule.name}
                onChange={(event) =>
                  updateRule(ruleIndex, { ...rule, name: event.target.value })
                }
                placeholder="Rule name (optional)"
                aria-label={`Targeting rule ${ruleIndex + 1} name`}
                maxLength={MAX_STRING_LENGTH}
                className="max-w-xs"
              />
              <span className="ml-auto flex items-center gap-1">
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={ruleIndex === 0}
                  onClick={() => moveRule(ruleIndex, ruleIndex - 1)}
                  aria-label={`Move targeting rule ${ruleIndex + 1} up`}
                >
                  <ArrowUp />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={ruleIndex === value.length - 1}
                  onClick={() => moveRule(ruleIndex, ruleIndex + 1)}
                  aria-label={`Move targeting rule ${ruleIndex + 1} down`}
                >
                  <ArrowDown />
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  onClick={() => onChange(value.filter((_, index) => index !== ruleIndex))}
                  aria-label={`Remove targeting rule ${ruleIndex + 1}`}
                >
                  <Trash2 />
                </Button>
              </span>
            </div>
            <div className="space-y-2">
              {rule.conditions.length === 0 ? (
                <div className="flex items-center justify-between gap-3 rounded-md bg-muted/40 p-3">
                  <p className="text-xs text-muted-foreground">
                    No conditions — this rule matches everyone.
                  </p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      updateRule(ruleIndex, {
                        ...rule,
                        conditions: [emptyCondition()],
                      })
                    }
                  >
                    <Plus />
                    Add condition
                  </Button>
                </div>
              ) : (
                rule.conditions.map((condition, conditionIndex) => (
                  <ConditionRow
                    key={conditionIndex}
                    ruleIndex={ruleIndex}
                    conditionIndex={conditionIndex}
                    condition={condition}
                    onChange={(next) =>
                      updateRule(ruleIndex, {
                        ...rule,
                        conditions: rule.conditions.map((current, index) =>
                          index === conditionIndex ? next : current,
                        ),
                      })
                    }
                    onAddAfter={() =>
                      updateRule(ruleIndex, {
                        ...rule,
                        conditions: [
                          ...rule.conditions.slice(0, conditionIndex + 1),
                          emptyCondition(),
                          ...rule.conditions.slice(conditionIndex + 1),
                        ],
                      })
                    }
                    onRemove={() =>
                      updateRule(ruleIndex, {
                        ...rule,
                        conditions: rule.conditions.filter(
                          (_, index) => index !== conditionIndex,
                        ),
                      })
                    }
                    canAdd={rule.conditions.length < MAX_CONDITIONS_PER_RULE}
                  />
                ))
              )}
            </div>
          </div>
        ))
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onChange([...value, emptyRule()])}
        disabled={value.length >= MAX_RULES}
      >
        <Plus />
        Add targeting rule
      </Button>
      <p className="text-xs text-muted-foreground">
        Type selects the user attribute. Conditions inside a rule use AND; rules are checked
        top-down. Traffic allocation is controlled only by the percentage above.
      </p>
    </fieldset>
  )
}
