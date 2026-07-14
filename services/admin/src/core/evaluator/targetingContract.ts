/** Strict targeting semantics shared by every APDL evaluator runtime. */

export const MAX_RULES = 50
export const MAX_CONDITIONS_PER_RULE = 20
export const MAX_IDENTIFIER_LENGTH = 128
export const MAX_STRING_LENGTH = 256
export const MAX_MEMBERSHIP_VALUES = 100

export const NUMERIC_PATTERN =
  '^-?(?:0|[1-9][0-9]*)(?:\\.[0-9]+)?(?:[eE][+-]?[0-9]+)?$'

const NUMERIC_REGEX = new RegExp(NUMERIC_PATTERN)

export const EQUALITY_OPERATORS = new Set(['equals', 'not_equals'])
export const STRING_OPERATORS = new Set([
  'contains',
  'not_contains',
  'starts_with',
  'ends_with',
])
export const NUMERIC_OPERATORS = new Set(['gt', 'gte', 'lt', 'lte'])
export const MEMBERSHIP_OPERATORS = new Set(['in', 'not_in'])
export const PRESENCE_OPERATORS = new Set(['exists', 'not_exists'])
export const SUPPORTED_OPERATORS = new Set([
  ...EQUALITY_OPERATORS,
  ...STRING_OPERATORS,
  ...NUMERIC_OPERATORS,
  ...MEMBERSHIP_OPERATORS,
  ...PRESENCE_OPERATORS,
])

export type JsonScalar = string | number | boolean

export function isIdentifier(value: unknown): value is string {
  return (
    typeof value === 'string' && value.length > 0 && value.length <= MAX_IDENTIFIER_LENGTH
  )
}

export function isBoundedString(value: unknown): value is string {
  return typeof value === 'string' && value.length <= MAX_STRING_LENGTH
}

export function isJsonNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

export function isScalar(value: unknown): value is JsonScalar {
  return isBoundedString(value) || typeof value === 'boolean' || isJsonNumber(value)
}

export function scalarEqual(left: unknown, right: unknown): boolean {
  if (!isScalar(left) || !isScalar(right) || typeof left !== typeof right) {
    return false
  }
  return left === right
}

export function parseNumeric(value: unknown): number | null {
  if (isJsonNumber(value)) return value
  if (!isBoundedString(value)) return null

  const match = NUMERIC_REGEX.exec(value)
  if (match === null || match[0].length !== value.length) return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function isMembershipList(value: unknown): value is JsonScalar[] {
  return (
    Array.isArray(value) &&
    value.length > 0 &&
    value.length <= MAX_MEMBERSHIP_VALUES &&
    value.every(isScalar)
  )
}

export function isConditionValueValid(operator: string, value: unknown): boolean {
  if (EQUALITY_OPERATORS.has(operator)) return isScalar(value)
  if (STRING_OPERATORS.has(operator)) return isBoundedString(value)
  if (NUMERIC_OPERATORS.has(operator)) return parseNumeric(value) !== null
  if (MEMBERSHIP_OPERATORS.has(operator)) return isMembershipList(value)
  return false
}
