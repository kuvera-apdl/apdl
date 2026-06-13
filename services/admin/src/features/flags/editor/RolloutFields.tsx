import { useFormContext, type FieldPath } from 'react-hook-form'

import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import type { FlagFormValues } from './formModel'

const COMMON_BUCKETS = ['user_id', 'anonymous_id'] as const

interface RolloutFieldsProps {
  /** e.g. "fallthrough.rollout" or "rules.0.rollout" */
  pathPrefix: string
  percentageError?: string
  bucketByError?: string
}

export function RolloutFields({ pathPrefix, percentageError, bucketByError }: RolloutFieldsProps) {
  const { register, watch, setValue } = useFormContext<FlagFormValues>()
  const percentagePath = `${pathPrefix}.percentage` as FieldPath<FlagFormValues>
  const bucketByPath = `${pathPrefix}.bucket_by` as FieldPath<FlagFormValues>

  const percentage = watch(percentagePath) as number
  const bucketBy = (watch(bucketByPath) as string) ?? ''
  const isCustomBucket = !COMMON_BUCKETS.includes(bucketBy as (typeof COMMON_BUCKETS)[number])

  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="space-y-1.5">
        <Label>Rollout %</Label>
        <div className="flex items-center gap-2">
          <input
            type="range"
            min={0}
            max={100}
            step={1}
            value={Number.isFinite(percentage) ? percentage : 0}
            onChange={(event) =>
              setValue(percentagePath, Number(event.target.value) as never, { shouldDirty: true })
            }
            className="w-36 accent-foreground"
            aria-label="Rollout percentage slider"
          />
          <Input
            type="number"
            min={0}
            max={100}
            step="any"
            className="w-24 tabular-nums"
            {...register(percentagePath, { valueAsNumber: true })}
          />
        </div>
        {percentageError ? <p className="text-xs text-destructive">{percentageError}</p> : null}
      </div>
      <div className="space-y-1.5">
        <Label>Bucket by</Label>
        <div className="flex items-center gap-2">
          <Select
            value={isCustomBucket ? 'custom' : bucketBy}
            onChange={(event) => {
              const next = event.target.value
              setValue(bucketByPath, (next === 'custom' ? '' : next) as never, {
                shouldDirty: true,
                shouldValidate: true,
              })
            }}
            className="w-40"
            aria-label="Bucket by"
          >
            <option value="user_id">user_id</option>
            <option value="anonymous_id">anonymous_id</option>
            <option value="custom">custom attribute…</option>
          </Select>
          {isCustomBucket ? (
            <Input
              placeholder="attribute name"
              className="w-40"
              {...register(bucketByPath)}
            />
          ) : null}
        </div>
        {bucketByError ? <p className="text-xs text-destructive">{bucketByError}</p> : null}
      </div>
    </div>
  )
}
