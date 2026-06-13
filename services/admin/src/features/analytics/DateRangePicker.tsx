import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

import { lastDays, todayIso, type DateRange } from './selectorModel'

const PRESETS: { label: string; range: () => DateRange }[] = [
  { label: 'Today', range: () => ({ start_date: todayIso(), end_date: todayIso() }) },
  { label: '7d', range: () => lastDays(7) },
  { label: '30d', range: () => lastDays(30) },
  { label: '90d', range: () => lastDays(90) },
]

interface DateRangePickerProps {
  value: DateRange
  onChange: (next: DateRange) => void
}

export function DateRangePicker({ value, onChange }: DateRangePickerProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <div className="flex items-center gap-1">
        {PRESETS.map((preset) => {
          const range = preset.range()
          const active = range.start_date === value.start_date && range.end_date === value.end_date
          return (
            <button
              key={preset.label}
              type="button"
              onClick={() => onChange(range)}
              className={cn(
                'rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                active
                  ? 'border-foreground bg-foreground text-background'
                  : 'text-muted-foreground hover:bg-accent',
              )}
            >
              {preset.label}
            </button>
          )
        })}
      </div>
      <Input
        type="date"
        value={value.start_date}
        onChange={(event) => onChange({ ...value, start_date: event.target.value })}
        className="w-40"
        aria-label="Start date"
      />
      <span className="text-muted-foreground">→</span>
      <Input
        type="date"
        value={value.end_date}
        onChange={(event) => onChange({ ...value, end_date: event.target.value })}
        className="w-40"
        aria-label="End date"
      />
    </div>
  )
}
