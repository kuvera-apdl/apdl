// RuleBuilder (plan §5.3.3): card order = evaluation order; operator-adaptive
// value inputs (none for existence, chips for in/not_in, canonical decimals
// for numeric comparisons). Reordering is keyboard-operable via up/down.
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Controller, useFieldArray, useFormContext, type FieldPath } from 'react-hook-form'

import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  MAX_CONDITIONS_PER_RULE,
  MAX_RULES,
  MAX_STRING_LENGTH,
} from '@/core/evaluator/targetingContract'
import {
  TargetingAttributesDatalist,
  TargetingConditionFields,
} from '@/features/targeting/TargetingConditionFields'
import {
  emptyTargetingCondition,
  newTargetingRuleId,
} from '@/features/targeting/editorModel'

import {
  type ConditionFormValues,
  type FlagFormValues,
} from './formModel'
import { RolloutFields } from './RolloutFields'

function ConditionRow({ rulePath, index, onRemove }: { rulePath: string; index: number; onRemove: () => void }) {
  const { control, getFieldState, formState } = useFormContext<FlagFormValues>()
  const base = `${rulePath}.conditions.${index}`
  const attributeError = getFieldState(`${base}.attribute` as FieldPath<FlagFormValues>, formState).error
  const valueError = getFieldState(`${base}.value` as FieldPath<FlagFormValues>, formState).error
  const valuesError = getFieldState(`${base}.values` as FieldPath<FlagFormValues>, formState).error

  return (
    <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_10rem_minmax(0,1fr)_auto]">
      <Controller
        control={control}
        name={base as FieldPath<FlagFormValues>}
        render={({ field }) => (
          <TargetingConditionFields
            condition={field.value as ConditionFormValues}
            onChange={field.onChange}
            idPrefix={`flag-${base.replaceAll('.', '-')}`}
            ariaLabels={{
              attribute: 'Attribute',
              operator: 'Operator',
              value: 'Condition value',
              valueType: 'Condition value type',
              values: 'Condition values',
            }}
            errors={{
              attribute: attributeError?.message,
              value: valueError?.message,
              values: valuesError?.message,
            }}
          />
        )}
      />
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
        <Input
          placeholder="rule name (optional)"
          className="max-w-xs"
          maxLength={MAX_STRING_LENGTH}
          {...register(`rules.${index}.name`)}
        />
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
            disabled={conditions.fields.length >= MAX_CONDITIONS_PER_RULE}
            onClick={() => conditions.append(emptyTargetingCondition())}
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
      <TargetingAttributesDatalist />
      {fields.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No targeting rules — Initial rollout applies to everyone.
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
        disabled={fields.length >= MAX_RULES}
        onClick={() =>
          append({
            id: newTargetingRuleId(),
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
