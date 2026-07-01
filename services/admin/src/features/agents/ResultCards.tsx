// Renderers for persisted agent outputs (gap G3). The payloads are LLM-shaped
// and not canonicalized — known fields render as cards, everything stays
// inspectable via a raw-JSON details block (never trusted as HTML, §12).
import { JsonView } from '@/components/shared/JsonView'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

function field(record: Record<string, unknown>, ...names: string[]): string | null {
  for (const name of names) {
    const value = record[name]
    if (typeof value === 'string' && value.trim() !== '') return value
  }
  return null
}

const PRIORITY_STYLES: Record<string, string> = {
  P0: 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300',
  P1: 'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
}

interface ResultCardProps {
  item: unknown
  kind: 'insight' | 'experiment_design' | 'personalization' | 'feature_proposal' | 'changeset'
}

const KIND_TITLE_FIELDS: Record<ResultCardProps['kind'], string[]> = {
  insight: ['title', 'name', 'summary', 'insight'],
  experiment_design: ['hypothesis', 'title', 'name'],
  personalization: ['title', 'name', 'component', 'description'],
  feature_proposal: ['title', 'name'],
  changeset: ['title', 'name', 'proposal_id'],
}

const KIND_BODY_FIELDS: Record<ResultCardProps['kind'], string[]> = {
  insight: ['description', 'detail', 'evidence'],
  experiment_design: ['description', 'rationale', 'success_metric', 'metric'],
  personalization: ['rationale', 'description', 'change'],
  feature_proposal: ['rationale', 'description'],
  changeset: ['spec', 'description'],
}

export function ResultCard({ item, kind }: ResultCardProps) {
  const record = typeof item === 'object' && item !== null ? (item as Record<string, unknown>) : {}
  const title = field(record, ...KIND_TITLE_FIELDS[kind])
  const body = field(record, ...KIND_BODY_FIELDS[kind])
  const priority = field(record, 'priority')
  const severity = field(record, 'severity')
  const effort = field(record, 'effort')
  const flagKey = field(record, 'flag_key')
  const metric = field(record, 'primary_metric', 'metric', 'success_metric')

  return (
    <div className="space-y-1.5 rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm font-medium">{title ?? `Untitled ${kind.replace(/_/g, ' ')}`}</p>
        {priority ? (
          <Badge variant="outline" className={cn(PRIORITY_STYLES[priority])}>
            {priority}
          </Badge>
        ) : null}
        {severity ? <Badge variant="secondary">{severity}</Badge> : null}
        {effort ? <Badge variant="outline">effort: {effort}</Badge> : null}
      </div>
      {body && body !== title ? <p className="text-sm text-muted-foreground">{body}</p> : null}
      <p className="space-x-3 text-xs text-muted-foreground">
        {flagKey ? (
          <span>
            flag: <code className="font-mono">{flagKey}</code>
          </span>
        ) : null}
        {metric ? (
          <span>
            metric: <code className="font-mono">{metric}</code>
          </span>
        ) : null}
      </p>
      <details>
        <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
          Full payload
        </summary>
        <JsonView data={item} className="mt-1 max-h-56" />
      </details>
    </div>
  )
}

interface ResultListProps {
  label: string
  items: unknown[]
  kind: ResultCardProps['kind']
}

export function ResultList({ label, items, kind }: ResultListProps) {
  if (items.length === 0) return null
  return (
    <div className="space-y-2">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label} ({items.length})
      </p>
      {items.map((item, index) => (
        <ResultCard key={index} item={item} kind={kind} />
      ))}
    </div>
  )
}
