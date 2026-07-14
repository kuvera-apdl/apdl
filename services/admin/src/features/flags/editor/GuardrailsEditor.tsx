import { Plus, Trash2 } from 'lucide-react'
import { useFieldArray, useFormContext } from 'react-hook-form'

import type { GuardrailMetric } from '@/api/types/flags'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'

import { GUARDRAIL_PAIRING, type FlagFormValues } from './formModel'

export function GuardrailsEditor() {
  const { register, setValue, watch, control, formState } = useFormContext<FlagFormValues>()
  const { fields, append, remove } = useFieldArray({ control, name: 'guardrails' })

  return (
    <div className="space-y-3">
      {fields.map((field, index) => {
        const metric = watch(`guardrails.${index}.metric`)
        const rowErrors = formState.errors.guardrails?.[index]
        return (
          <div key={field.id} className="flex flex-wrap items-end gap-3 rounded-md border p-3">
            <div className="space-y-1.5">
              <Label>Metric</Label>
              <Select
                className="w-52"
                {...register(`guardrails.${index}.metric`, {
                  onChange: (event) => {
                    // The pairing is server-enforced — lock it in the form.
                    const next = event.target.value as GuardrailMetric
                    setValue(`guardrails.${index}.threshold`, GUARDRAIL_PAIRING[next], {
                      shouldDirty: true,
                    })
                  },
                })}
              >
                <option value="frontend_error_rate">frontend_error_rate</option>
                <option value="frontend_error_count">frontend_error_count</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label>Threshold</Label>
              <Input value={GUARDRAIL_PAIRING[metric]} readOnly disabled className="w-36 font-mono text-xs" />
            </div>
            <div className="space-y-1.5">
              <Label>Scope</Label>
              <Input placeholder="project-wide" className="w-36" {...register(`guardrails.${index}.scope`)} />
            </div>
            <div className="space-y-1.5">
              <Label>Min. exposures</Label>
              <Input
                type="number"
                min={0}
                step={1}
                className="w-28 tabular-nums"
                {...register(`guardrails.${index}.minimum_exposures`, { valueAsNumber: true })}
              />
              {rowErrors?.minimum_exposures ? (
                <p className="text-xs text-destructive">{rowErrors.minimum_exposures.message}</p>
              ) : null}
            </div>
            <div className="space-y-1.5">
              <Label>Window (min)</Label>
              <Input
                type="number"
                min={1}
                max={129600}
                step={1}
                className="w-24 tabular-nums"
                {...register(`guardrails.${index}.window_minutes`, { valueAsNumber: true })}
              />
              {rowErrors?.window_minutes ? (
                <p className="text-xs text-destructive">{rowErrors.window_minutes.message}</p>
              ) : null}
            </div>
            <Button type="button" variant="ghost" size="icon" onClick={() => remove(index)} aria-label="Remove guardrail">
              <Trash2 />
            </Button>
          </div>
        )
      })}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() =>
          append({
            metric: 'frontend_error_rate',
            threshold: '2x_baseline',
            scope: '',
            minimum_exposures: 100,
            window_minutes: 10,
          })
        }
      >
        <Plus />
        Add guardrail
      </Button>
      <p className="text-xs text-muted-foreground">
        Guardrails are read-only diagnostics in the OSS developer preview. Each metric has one
        valid threshold; automatic flag mutation is unavailable.
      </p>
    </div>
  )
}
