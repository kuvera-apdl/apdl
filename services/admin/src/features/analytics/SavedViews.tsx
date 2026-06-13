// Saved analytics views (plan D5): named query configs in localStorage, per
// workspace — no server persistence by design.
import { Bookmark, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { useWorkspace } from '@/core/workspace'

interface SavedView<T> {
  name: string
  view: T
}

function storageKey(workspaceId: string, screen: string): string {
  return `apdl-admin:views:${workspaceId}:${screen}`
}

function loadViews<T>(workspaceId: string, screen: string): SavedView<T>[] {
  try {
    const raw = localStorage.getItem(storageKey(workspaceId, screen))
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? (parsed as SavedView<T>[]) : []
  } catch {
    return []
  }
}

interface SavedViewsProps<T> {
  screen: string
  current: T
  onLoad: (view: T) => void
}

export function SavedViews<T>({ screen, current, onLoad }: SavedViewsProps<T>) {
  const { active } = useWorkspace()
  const workspaceId = active?.id ?? 'none'
  const [views, setViews] = useState<SavedView<T>[]>(() => loadViews<T>(workspaceId, screen))
  const [saveOpen, setSaveOpen] = useState(false)
  const [name, setName] = useState('')

  const persist = (next: SavedView<T>[]) => {
    setViews(next)
    localStorage.setItem(storageKey(workspaceId, screen), JSON.stringify(next))
  }

  const saveCurrent = () => {
    const trimmed = name.trim()
    if (!trimmed) return
    persist([...views.filter((view) => view.name !== trimmed), { name: trimmed, view: current }])
    setSaveOpen(false)
    setName('')
    toast.success(`View "${trimmed}" saved`)
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" size="sm">
            <Bookmark />
            Views
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuLabel>Saved views (this browser)</DropdownMenuLabel>
          {views.length === 0 ? (
            <DropdownMenuItem disabled>None yet</DropdownMenuItem>
          ) : (
            views.map((view) => (
              <DropdownMenuItem key={view.name} onSelect={() => onLoad(view.view)}>
                <span className="flex-1">{view.name}</span>
                <button
                  type="button"
                  aria-label={`Delete view ${view.name}`}
                  className="text-muted-foreground hover:text-destructive"
                  onClick={(event) => {
                    event.stopPropagation()
                    event.preventDefault()
                    persist(views.filter((entry) => entry.name !== view.name))
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </DropdownMenuItem>
            ))
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setSaveOpen(true)}>Save current…</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Save view</DialogTitle>
            <DialogDescription>Stored locally in this browser, per workspace.</DialogDescription>
          </DialogHeader>
          <Input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="View name"
            aria-label="View name"
            onKeyDown={(event) => {
              if (event.key === 'Enter') saveCurrent()
            }}
          />
          <DialogFooter>
            <Button variant="outline" onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button onClick={saveCurrent} disabled={name.trim() === ''}>
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
