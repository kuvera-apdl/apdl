// Pre-submit review (plan §5.3.3): the canonical JSON payload and the exact
// curl equivalent — saving from review submits.
import { Loader2 } from 'lucide-react'

import { CopyButton } from '@/components/shared/CopyButton'
import { JsonView } from '@/components/shared/JsonView'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { toCurl, type CurlSpec } from '@/lib/curl'

interface ReviewSheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  description: string
  payload: unknown
  curl: CurlSpec
  error: string | null
  submitting: boolean
  confirmLabel: string
  onConfirm: () => void
}

export function ReviewSheet({
  open,
  onOpenChange,
  title,
  description,
  payload,
  curl,
  error,
  submitting,
  confirmLabel,
  onConfirm,
}: ReviewSheetProps) {
  const command = toCurl(curl)
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div>
            <p className="mb-1 text-xs font-medium text-muted-foreground">Canonical payload</p>
            <JsonView data={payload} className="max-h-64" />
          </div>
          <div className="relative">
            <p className="mb-1 text-xs font-medium text-muted-foreground">As curl</p>
            <pre className="max-h-40 overflow-auto rounded-md bg-muted p-3 pr-10 font-mono text-xs leading-relaxed">
              {command}
            </pre>
            <CopyButton value={command} label="Copy command" className="absolute right-2 top-6" />
          </div>
          {error ? (
            <p className="rounded-md border border-destructive/50 bg-destructive/10 p-2 text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>
            Back to editing
          </Button>
          <Button onClick={onConfirm} disabled={submitting}>
            {submitting ? <Loader2 className="animate-spin" /> : null}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
