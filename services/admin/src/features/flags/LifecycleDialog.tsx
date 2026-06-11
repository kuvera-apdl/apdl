// Lifecycle actions (plan §5.3.4): every dialog states the recorded actor and
// the consequence, and shows the exact API call it will make.
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { archiveFlagCurl, cleanupFlagCurl, disableFlagCurl, updateFlagCurl } from '@/api/config'
import { ApiError } from '@/api/http'
import type { FlagConfig, FlagDisable } from '@/api/types/flags'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import {
  useArchiveFlagMutation,
  useCleanupFlagMutation,
  useDisableFlagMutation,
  useUpdateFlagMutation,
} from '@/features/flags/mutations'
import { toCurl, type CurlSpec } from '@/lib/curl'
import { formatPercent } from '@/lib/format'

export type LifecycleAction = 'activate' | 'deactivate' | 'disable' | 'archive' | 'cleanup'

/** Client mirror of the server's _is_cleanup_candidate (routers/admin.py). */
export function isCleanupCandidate(flag: FlagConfig): boolean {
  if (flag.state !== 'active' || !flag.enabled) return false
  if (flag.rules.length > 0) return false
  if (flag.fallthrough.rollout.percentage < 100) return false
  const positive = flag.variants.filter((variant) => variant.weight > 0)
  return positive.length === 1 && positive[0].key !== flag.default_variant
}

/** Which lifecycle actions the UI offers for a flag in its current state. */
export function availableActions(flag: FlagConfig): LifecycleAction[] {
  if (flag.state === 'archived') return []
  const actions: LifecycleAction[] = []
  if (flag.state === 'draft' || flag.state === 'disabled') actions.push('activate')
  if (flag.state === 'active') actions.push('deactivate', 'disable')
  if (isCleanupCandidate(flag)) actions.push('cleanup')
  actions.push('archive')
  return actions
}

export const ACTION_LABELS: Record<LifecycleAction, string> = {
  activate: 'Activate',
  deactivate: 'Deactivate to draft',
  disable: 'Disable (kill switch)…',
  archive: 'Archive…',
  cleanup: 'Clean up…',
}

interface LifecycleDialogProps {
  flag: FlagConfig
  action: LifecycleAction
  onClose: () => void
}

function CurlPreview({ spec }: { spec: CurlSpec }) {
  return (
    <pre className="max-h-40 overflow-auto rounded-md bg-muted p-3 font-mono text-xs leading-relaxed">
      {toCurl(spec)}
    </pre>
  )
}

export function LifecycleDialog({ flag, action, onClose }: LifecycleDialogProps) {
  const { active } = useWorkspace()
  const conn = active ? serviceConnection(active, 'config') : null

  const updateMutation = useUpdateFlagMutation(flag.key)
  const disableMutation = useDisableFlagMutation(flag.key)
  const archiveMutation = useArchiveFlagMutation(flag.key)
  const cleanupMutation = useCleanupFlagMutation(flag.key)

  const [error, setError] = useState<string | null>(null)
  const [disableReason, setDisableReason] = useState<FlagDisable['reason']>('guardrail_failed')
  const [note, setNote] = useState('')
  const [confirmKey, setConfirmKey] = useState('')

  const submitting =
    updateMutation.isPending ||
    disableMutation.isPending ||
    archiveMutation.isPending ||
    cleanupMutation.isPending

  if (!conn) return null

  const disableBody: FlagDisable = {
    reason: disableReason,
    source: 'admin',
    evidence: note.trim() ? { note: note.trim() } : {},
  }
  const cleanupBody = {
    version: flag.version,
    source: 'admin' as const,
    evidence: note.trim() ? { note: note.trim() } : {},
  }

  const run = async () => {
    setError(null)
    try {
      if (action === 'activate') {
        await updateMutation.mutateAsync({ version: flag.version, state: 'active' })
        toast.success(`"${flag.key}" activated`)
      } else if (action === 'deactivate') {
        await updateMutation.mutateAsync({ version: flag.version, state: 'draft' })
        toast.success(`"${flag.key}" deactivated to draft`)
      } else if (action === 'disable') {
        await disableMutation.mutateAsync(disableBody)
        toast.success(`"${flag.key}" disabled — SDKs fall back to defaults`)
      } else if (action === 'archive') {
        await archiveMutation.mutateAsync()
        toast.success(`"${flag.key}" archived`)
      } else {
        const response = await cleanupMutation.mutateAsync(cleanupBody)
        toast.success(`"${flag.key}" cleaned up (${response.cleanup_reasons.join(', ')})`)
      }
      onClose()
    } catch (caught) {
      // 409s are semantic (version conflict, ineligible cleanup) — show the
      // server's message verbatim (§4.4).
      setError(caught instanceof ApiError ? caught.message : 'Request failed')
    }
  }

  const content = (() => {
    switch (action) {
      case 'activate':
        return {
          title: `Activate ${flag.key}?`,
          description: `Activating exposes ~${formatPercent(flag.fallthrough.rollout.percentage)} of fallthrough traffic (plus any rule rollouts) on the next SSE push.`,
          curl: updateFlagCurl(conn, flag.key, { version: flag.version, state: 'active' }),
          confirmLabel: 'Activate',
          destructive: false,
          disabled: false,
        }
      case 'deactivate':
        return {
          title: `Deactivate ${flag.key} to draft?`,
          description:
            'Paused by owner — distinct from the kill switch: no disable metadata is recorded, and the flag stops serving on the next SSE push.',
          curl: updateFlagCurl(conn, flag.key, { version: flag.version, state: 'draft' }),
          confirmLabel: 'Deactivate',
          destructive: false,
          disabled: false,
        }
      case 'disable':
        return {
          title: `Disable ${flag.key} (kill switch)?`,
          description:
            'All clients fall back to default behavior on the next SSE push. Disable metadata and your evidence note are recorded in the audit trail.',
          curl: disableFlagCurl(conn, flag.key, disableBody),
          confirmLabel: 'Disable flag',
          destructive: true,
          disabled: false,
        }
      case 'archive':
        return {
          title: `Archive ${flag.key}?`,
          description:
            'Irreversible — archived is a terminal state. The flag stops serving and disappears from SDK payloads; history remains under "show archived".',
          curl: archiveFlagCurl(conn, flag.key),
          confirmLabel: 'Archive forever',
          destructive: true,
          disabled: confirmKey !== flag.key,
        }
      case 'cleanup':
        return {
          title: `Clean up ${flag.key}?`,
          description:
            'Archives a fully-rolled-out flag through the cleanup workflow: active, no rules, 100% fallthrough, and a single winning variant. The server re-checks eligibility.',
          curl: cleanupFlagCurl(conn, flag.key, cleanupBody),
          confirmLabel: 'Clean up & archive',
          destructive: true,
          disabled: false,
        }
    }
  })()

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{content.title}</DialogTitle>
          <DialogDescription>{content.description}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          {action === 'disable' ? (
            <>
              <div className="space-y-1.5">
                <Label>Reason</Label>
                <Select
                  value={disableReason}
                  onChange={(event) => setDisableReason(event.target.value as FlagDisable['reason'])}
                >
                  <option value="guardrail_failed">guardrail_failed</option>
                  <option value="experiment_rollback">experiment_rollback</option>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>Evidence note (optional)</Label>
                <Input
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                  placeholder="What prompted this kill switch?"
                />
              </div>
            </>
          ) : null}
          {action === 'cleanup' ? (
            <div className="space-y-1.5">
              <Label>Note (optional)</Label>
              <Input
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="Recorded in the audit evidence"
              />
            </div>
          ) : null}
          {action === 'archive' ? (
            <div className="space-y-1.5">
              <Label>
                Type <code className="font-mono text-xs">{flag.key}</code> to confirm
              </Label>
              <Input
                value={confirmKey}
                onChange={(event) => setConfirmKey(event.target.value)}
                className="font-mono text-xs"
                aria-label="Confirm flag key"
              />
            </div>
          ) : null}
          <CurlPreview spec={content.curl} />
          <p className="text-xs text-muted-foreground">
            Recorded actor: <span className="font-medium text-foreground">{active?.actor}</span> ·
            optimistic lock v{flag.version}
          </p>
          {error ? (
            <p className="rounded-md border border-destructive/50 bg-destructive/10 p-2 text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            variant={content.destructive ? 'destructive' : 'default'}
            onClick={() => void run()}
            disabled={content.disabled || submitting}
          >
            {submitting ? <Loader2 className="animate-spin" /> : null}
            {content.confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
