import type { ConditionOperator, GateCondition } from '@/api/types/flags'
import {
  EQUALITY_OPERATORS,
  MEMBERSHIP_OPERATORS,
  NUMERIC_OPERATORS,
  PRESENCE_OPERATORS,
  isScalar,
  parseNumeric,
  type JsonScalar,
} from '@/core/evaluator/targetingContract'

export const TARGETING_OPERATOR_GROUPS: ReadonlyArray<{
  label: string
  operators: readonly ConditionOperator[]
}> = [
  { label: 'Existence', operators: ['exists', 'not_exists'] },
  { label: 'Equality', operators: ['equals', 'not_equals'] },
  { label: 'String', operators: ['contains', 'not_contains', 'starts_with', 'ends_with'] },
  { label: 'Numeric', operators: ['gt', 'gte', 'lt', 'lte'] },
  { label: 'Collection', operators: ['in', 'not_in'] },
]

export const COMMON_TARGETING_ATTRIBUTES = [
  'user_id',
  'anonymous_id',
  'plan',
  'country',
  'email',
  'device',
  'beta_opt_in',
] as const

export const TARGETING_ATTRIBUTES_DATALIST_ID = 'apdl-targeting-attributes'

export type ScalarValueType = 'string' | 'number' | 'boolean'

/**
 * Editor-only condition shape. `valueType` selects the JSON scalar written to
 * the canonical GateCondition `value`; it is never part of the wire contract.
 */
export interface TargetingConditionFormValue {
  attribute: string
  operator: ConditionOperator
  valueType: ScalarValueType
  value: JsonScalar
  values: JsonScalar[]
}

function compactUuid(): string {
  return crypto.randomUUID().replace(/-/g, '').slice(0, 12)
}

export function newTargetingRuleId(): string {
  return `rule_${compactUuid()}`
}

export function newTargetingConditionUiId(): string {
  return `condition_${compactUuid()}`
}

export function scalarValueType(value: JsonScalar): ScalarValueType {
  if (typeof value === 'number') return 'number'
  if (typeof value === 'boolean') return 'boolean'
  return 'string'
}

export function targetingConditionToFormValue(
  condition: GateCondition,
): TargetingConditionFormValue {
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
    valueType: NUMERIC_OPERATORS.has(condition.operator) ? 'number' : scalarValueType(value),
    value,
    values: [],
  }
}

/** Project editor-only state to the one strict GateCondition wire shape. */
export function targetingConditionToWire(
  condition: TargetingConditionFormValue,
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
    value: typeof condition.value === 'string' ? condition.value : String(condition.value),
  }
}

export function emptyTargetingCondition(): TargetingConditionFormValue {
  return {
    attribute: '',
    operator: 'equals',
    valueType: 'string',
    value: '',
    values: [],
  }
}

export function targetingConditionForOperator(
  condition: TargetingConditionFormValue,
  operator: ConditionOperator,
): TargetingConditionFormValue {
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
      MEMBERSHIP_OPERATORS.has(condition.operator) && condition.valueType === 'boolean'
        ? false
        : condition.value,
    values: [],
  }
}
