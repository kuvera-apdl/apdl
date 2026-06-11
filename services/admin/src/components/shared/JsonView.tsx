import { cn } from '@/lib/utils'

interface JsonViewProps {
  data: unknown
  className?: string
}

// Inert JSON rendering — server strings are always text, never HTML (§12).
export function JsonView({ data, className }: JsonViewProps) {
  return (
    <pre
      className={cn(
        'max-h-80 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed',
        className,
      )}
    >
      {JSON.stringify(data, null, 2)}
    </pre>
  )
}
