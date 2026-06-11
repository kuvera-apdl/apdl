import { Plus, Trash2 } from 'lucide-react'
import { useFieldArray, useFormContext } from 'react-hook-form'

import { VariantSplitBar } from '@/components/shared/VariantSplitBar'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import type { FlagFormValues } from './formModel'

export function VariantsEditor() {
  const {
    register,
    watch,
    control,
    formState: { errors },
  } = useFormContext<FlagFormValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'variants' })

  const variants = watch('variants')
  const defaultVariant = watch('default_variant')
  const validForPreview = variants.every(
    (variant) => variant.key.trim() !== '' && Number.isFinite(variant.weight) && variant.weight >= 0,
  )

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        {fields.map((field, index) => {
          const rowErrors = errors.variants?.[index]
          return (
            <div key={field.id} className="flex items-start gap-2">
              <div className="w-56">
                <Input
                  placeholder="variant key"
                  className="font-mono text-xs"
                  {...register(`variants.${index}.key`)}
                />
                {rowErrors?.key ? (
                  <p className="mt-1 text-xs text-destructive">{rowErrors.key.message}</p>
                ) : null}
              </div>
              <div className="w-28">
                <Input
                  type="number"
                  min={0}
                  step={1}
                  placeholder="weight"
                  className="tabular-nums"
                  {...register(`variants.${index}.weight`, { valueAsNumber: true })}
                />
                {rowErrors?.weight ? (
                  <p className="mt-1 text-xs text-destructive">{rowErrors.weight.message}</p>
                ) : null}
              </div>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => remove(index)}
                disabled={fields.length <= 1}
                aria-label={`Remove variant ${index + 1}`}
              >
                <Trash2 />
              </Button>
            </div>
          )
        })}
        {typeof errors.variants?.message === 'string' ? (
          <p className="text-xs text-destructive">{errors.variants.message}</p>
        ) : null}
        {typeof errors.variants?.root?.message === 'string' ? (
          <p className="text-xs text-destructive">{errors.variants.root.message}</p>
        ) : null}
        <Button type="button" variant="outline" size="sm" onClick={() => append({ key: '', weight: 1 })}>
          <Plus />
          Add variant
        </Button>
      </div>

      <div className="max-w-xs space-y-1.5">
        <Label>Default variant</Label>
        <Select {...register('default_variant')} aria-label="Default variant">
          {variants.map((variant, index) => (
            <option key={`${variant.key}-${index}`} value={variant.key}>
              {variant.key || '(unnamed)'}
            </option>
          ))}
        </Select>
        {errors.default_variant ? (
          <p className="text-xs text-destructive">{errors.default_variant.message}</p>
        ) : null}
        <p className="text-xs text-muted-foreground">
          Served when evaluation misses every rollout; must match a variant key.
        </p>
      </div>

      {validForPreview ? (
        <div className="max-w-md">
          <VariantSplitBar variants={variants} defaultVariant={defaultVariant} />
        </div>
      ) : null}
    </div>
  )
}
