import { AlertCircle, Inbox } from 'lucide-react'
import type { ReactNode } from 'react'

import { ApiError } from '@/api/http'
import { Button } from '@/components/ui/button'

interface EmptyStateProps {
  icon?: ReactNode
  title: string
  description?: string
  children?: ReactNode
}

export function EmptyState({ icon, title, description, children }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      <div className="text-muted-foreground">{icon ?? <Inbox className="h-8 w-8" />}</div>
      <p className="font-medium">{title}</p>
      {description ? <p className="max-w-md text-sm text-muted-foreground">{description}</p> : null}
      {children ? <div className="mt-2 flex items-center gap-2">{children}</div> : null}
    </div>
  )
}

interface ErrorStateProps {
  error: Error
  onRetry?: () => void
}

export function ErrorState({ error, onRetry }: ErrorStateProps) {
  const code = error instanceof ApiError ? error.code : null
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center">
      <AlertCircle className="h-8 w-8 text-destructive" />
      <p className="font-medium">Request failed</p>
      <p className="max-w-md break-words text-sm text-muted-foreground">
        {code ? <code className="mr-1 rounded bg-muted px-1 py-0.5 text-xs">{code}</code> : null}
        {error.message}
      </p>
      {onRetry ? (
        <Button variant="outline" size="sm" className="mt-2" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </div>
  )
}
