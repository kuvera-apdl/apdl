// 409 version-conflict dialog (plan §4.4 / §5.3.3): show what changed on the
// server since the flag was loaded, plus the fields this submit was changing,
// then rebase or discard.
import type { FlagConfig, FlagUpdate } from '@/api/types/flags'
import { JsonDiff } from '@/components/shared/JsonDiff'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

interface ConflictDialogProps {
  open: boolean
  baseFlag: FlagConfig
  currentFlag: FlagConfig | null
  pendingUpdate: FlagUpdate
  onRebase: () => void
  onDiscard: () => void
  onClose: () => void
}

export function ConflictDialog({
  open,
  baseFlag,
  currentFlag,
  pendingUpdate,
  onRebase,
  onDiscard,
  onClose,
}: ConflictDialogProps) {
  const myFields = Object.keys(pendingUpdate).filter((field) => field !== 'version')
  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Version conflict</DialogTitle>
          <DialogDescription>
            You edited v{baseFlag.version}, but the flag is now at v
            {currentFlag?.version ?? '?'} — someone (or something) changed it while you were
            editing.
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[50vh] space-y-4 overflow-auto">
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">
              Changed on the server since you loaded
            </p>
            {currentFlag ? (
              <JsonDiff
                before={baseFlag as unknown as Record<string, unknown>}
                after={currentFlag as unknown as Record<string, unknown>}
              />
            ) : (
              <p className="text-sm text-muted-foreground">
                Could not load the current flag — it may have been archived.
              </p>
            )}
          </div>
          <div>
            <p className="mb-2 text-xs font-medium text-muted-foreground">Your pending changes</p>
            <div className="flex flex-wrap gap-1.5">
              {myFields.map((field) => (
                <Badge key={field} variant="secondary" className="font-mono">
                  {field}
                </Badge>
              ))}
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onDiscard}>
            Discard my edits
          </Button>
          <Button onClick={onRebase} disabled={!currentFlag}>
            Rebase my edits onto v{currentFlag?.version ?? '?'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
