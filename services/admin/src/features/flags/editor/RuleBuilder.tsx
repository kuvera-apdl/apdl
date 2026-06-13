// RuleBuilder (plan §5.3.3): card order = evaluation order; operator-adaptive
// value inputs (none for existence, chips for in/not_in, numbers, live regex
// validity). Reordering is keyboard-operable via the up/down buttons.
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Controller, useFieldArray, useFormContext, type FieldPath } from 'react-hook-form'

import type { ConditionOperator } from '@/api/types/flags'
import { TagInput } from '@/components/shared/TagInput'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Select } from '@/components/ui/select'

import {
  EXISTENCE_OPERATORS,
  LIST_OPERATORS,
  NUMERIC_OPERATORS,
  newRuleId,
  type FlagFormValues,
} from './formModel'
import { RolloutFields } from './RolloutFields'

const OPERATOR_GROUPS: { label: string; operators: ConditionOperator[] }[] = [
  { label: 'Existence', operators: ['exists', 'not_exists'] },
  { label: 'Equality', operators: ['equals', 'not_equals'] },
  { label: 'String', operators: ['contains', 'not_contains', 'starts_with', 'ends_with'] },
  { label: 'Numeric', operators: ['gt', 'gte', 'lt', 'lte'] },
  { label: 'Collection', operators: ['in', 'not_in'] },
  { label: 'Regex', operators: ['regex'] },
]

const COMMON_ATTRIBUTES = ['user_id', 'anonymous_id', 'plan', 'country', 'email', 'device', 'beta_opt_in']

function ConditionRow({ rulePath, index, onRemove }: { rulePath: string; index: number; onRemove: () => void }) {
  const { register, watch, control, getFieldState, formState } = useFormContext<FlagFormValues>()
  const base = `${rulePath}.conditions.${index}`
  const operator = watch(`${base}.operator` as FieldPath<FlagFormValues>) as ConditionOperator
  const attributeError = getFieldState(`${base}.attribute` as FieldPath<FlagFormValues>, formState).error
  const valueError = getFieldState(`${base}.value` as FieldPath<FlagFormValues>, formState).error
  const valuesError = getFieldState(`${base}.values` as FieldPath<FlagFormValues>, formState).error

  return (
    <div className="flex flex-wrap items-start gap-2">
      <div className="w-44">
        <Input
          placeholder="attribute"
          className="font-mono text-xs"
          list="apdl-common-attributes"
          {...register(`${base}.attribute` as FieldPath<FlagFormValues>)}
        />
        {attributeError ? <p className="mt-1 text-xs text-destructive">{attributeError.message}</p> : null}
      </div>
      <Select
        className="w-36"
        aria-label="Operator"
        {...register(`${base}.operator` as FieldPath<FlagFormValues>)}
      >
        {OPERATOR_GROUPS.map((group) => (
          <optgroup key={group.label} label={group.label}>
            {group.operators.map((op) => (
              <option key={op} value={op}>
                {op.replace(/_/g, ' ')}
              </option>
            ))}
          </optgroup>
        ))}
      </Select>
      <div className="min-w-44 flex-1">
        {EXISTENCE_OPERATORS.has(operator) ? (
          <p className="py-2 text-xs text-muted-foreground">no value — checks presence</p>
        ) : LIST_OPERATORS.has(operator) ? (
          <>
            <Controller
              control={control}
              name={`${base}.values` as FieldPath<FlagFormValues>}
              render={({ field }) => (
                <TagInput
                  value={(field.value as string[]) ?? []}
                  onChange={field.onChange}
                  placeholder="add value, press Enter"
                  aria-label="Condition values"
                />
              )}
            />
            {valuesError ? <p className="mt-1 text-xs text-destructive">{valuesError.message}</p> : null}
            <p className="mt-1 text-xs text-muted-foreground">compared as exact string values</p>
          </>
        ) : (
          <>
            <Input
              placeholder={NUMERIC_OPERATORS.has(operator) ? 'number' : operator === 'regex' ? 'pattern' : 'value'}
              type={NUMERIC_OPERATORS.has(operator) ? 'number' : 'text'}
              step="any"
              className={operator === 'regex' ? 'font-mono text-xs' : undefined}
              {...register(`${base}.value` as FieldPath<FlagFormValues>)}
            />
            {valueError ? <p className="mt-1 text-xs text-destructive">{valueError.message}</p> : null}
          </>
        )}
      </div>
      <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label="Remove condition">
        <Trash2 />
      </Button>
    </div>
  )
}

function RuleCard({ index, total, onMove, onRemove }: { index: number; total: number; onMove: (from: number, to: number) => void; onRemove: () => void }) {
  const { register, control } = useFormContext<FlagFormValues>()
  const rulePath = `rules.${index}`
  const conditions = useFieldArray({ control, name: `rules.${index}.conditions` as 'rules.0.conditions' })

  return (
    <Card>
      <CardHeader className="flex-row items-center gap-2 space-y-0 pb-3">
        <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
          {index + 1}
        </span>
        <Input placeholder="rule name (optional)" className="max-w-xs" {...register(`rules.${index}.name`)} />
        <span className="ml-auto flex items-center gap-1">
          <Button type="button" variant="ghost" size="icon" disabled={index === 0} onClick={() => onMove(index, index - 1)} aria-label="Move rule up">
            <ArrowUp />
          </Button>
          <Button type="button" variant="ghost" size="icon" disabled={index === total - 1} onClick={() => onMove(index, index + 1)} aria-label="Move rule down">
            <ArrowDown />
          </Button>
          <Button type="button" variant="ghost" size="icon" onClick={onRemove} aria-label="Remove rule">
            <Trash2 />
          </Button>
        </span>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-2">
          {conditions.fields.length === 0 ? (
            <p className="text-xs text-muted-foreground">No conditions — this rule matches every user.</p>
          ) : (
            conditions.fields.map((field, conditionIndex) => (
              <ConditionRow
                key={field.id}
                rulePath={rulePath}
                index={conditionIndex}
                onRemove={() => conditions.remove(conditionIndex)}
              />
            ))
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => conditions.append({ attribute: '', operator: 'equals', value: '', values: [] })}
          >
            <Plus />
            Add condition
          </Button>
        </div>
        <div className="border-t pt-3">
          <RolloutFields pathPrefix={`${rulePath}.rollout`} />
        </div>
      </CardContent>
    </Card>
  )
}

export function RuleBuilder() {
  const { control } = useFormContext<FlagFormValues>()
  const { fields, append, remove, move } = useFieldArray({ control, name: 'rules' })

  return (
    <div className="space-y-3">
      <datalist id="apdl-common-attributes">
        {COMMON_ATTRIBUTES.map((attribute) => (
          <option key={attribute} value={attribute} />
        ))}
      </datalist>
      {fields.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No targeting rules — all traffic goes straight to fallthrough.
        </p>
      ) : (
        fields.map((field, index) => (
          <RuleCard
            key={field.id}
            index={index}
            total={fields.length}
            onMove={move}
            onRemove={() => remove(index)}
          />
        ))
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() =>
          append({
            id: newRuleId(),
            name: '',
            conditions: [],
            rollout: { percentage: 100, bucket_by: 'user_id' },
          })
        }
      >
        <Plus />
        Add rule
      </Button>
      <p className="text-xs text-muted-foreground">
        Rules evaluate top-down; the first rule whose conditions all match wins.
      </p>
    </div>
  )
}
