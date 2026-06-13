// Structured before/after diff for audit entries: changed top-level keys only.
import { cn } from '@/lib/utils'

export type DiffKind = 'added' | 'removed' | 'changed'

export interface DiffEntry {
  key: string
  kind: DiffKind
  before: unknown
  after: unknown
}

export function diffObjects(
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
): DiffEntry[] {
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])
  const entries: DiffEntry[] = []
  for (const key of [...keys].sort()) {
    const hasBefore = before !== null && key in before
    const hasAfter = after !== null && key in after
    const beforeValue = hasBefore ? before[key] : undefined
    const afterValue = hasAfter ? after[key] : undefined
    if (hasBefore && hasAfter) {
      if (JSON.stringify(beforeValue) !== JSON.stringify(afterValue)) {
        entries.push({ key, kind: 'changed', before: beforeValue, after: afterValue })
      }
    } else if (hasBefore) {
      entries.push({ key, kind: 'removed', before: beforeValue, after: undefined })
    } else {
      entries.push({ key, kind: 'added', before: undefined, after: afterValue })
    }
  }
  return entries
}

const MAX_VALUE_LENGTH = 600

function renderValue(value: unknown): string {
  if (value === undefined) return '—'
  const text = JSON.stringify(value, null, 2) ?? 'undefined'
  return text.length > MAX_VALUE_LENGTH ? `${text.slice(0, MAX_VALUE_LENGTH)}…` : text
}

const KIND_LABELS: Record<DiffKind, string> = {
  added: 'added',
  removed: 'removed',
  changed: 'changed',
}

interface JsonDiffProps {
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  className?: string
}

export function JsonDiff({ before, after, className }: JsonDiffProps) {
  const entries = diffObjects(before, after)
  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground">No field-level changes.</p>
  }
  return (
    <div className={cn('space-y-3', className)}>
      {entries.map((entry) => (
        <div key={entry.key} className="space-y-1.5">
          <div className="flex items-center gap-2">
            <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{entry.key}</code>
            <span className="text-xs text-muted-foreground">{KIND_LABELS[entry.kind]}</span>
          </div>
          <div className="grid gap-1.5 md:grid-cols-2">
            {entry.kind !== 'added' ? (
              <pre className="overflow-auto rounded-md border border-red-200 bg-red-50 p-2 font-mono text-xs dark:border-red-900 dark:bg-red-950/40">
                {renderValue(entry.before)}
              </pre>
            ) : (
              <div />
            )}
            {entry.kind !== 'removed' ? (
              <pre className="overflow-auto rounded-md border border-emerald-200 bg-emerald-50 p-2 font-mono text-xs dark:border-emerald-900 dark:bg-emerald-950/40">
                {renderValue(entry.after)}
              </pre>
            ) : (
              <div />
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
