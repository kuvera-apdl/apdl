// List-only event picker. Following the kit's lean convention (see ui/select),
// this avoids a Radix popover/cmdk dependency: a button trigger opens a panel
// with a filter box and the discovered event names from POST
// /v1/query/events/names. Selection is restricted to the list — the filter only
// narrows it. The current value is always shown, even if it predates the catalog
// window or the catalog failed to load.
import { Check, ChevronsUpDown, X } from 'lucide-react'
import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'

import { cn } from '@/lib/utils'

import { useEventCatalog } from './useEventCatalog'

interface EventComboboxProps {
  value: string
  onChange: (next: string) => void
  ariaLabel?: string
  /** Render a clear (×) control when a value is selected. */
  clearable?: boolean
  placeholder?: string
  /** Applied to the root wrapper — set width / inline layout here. */
  className?: string
  /** Applied to the trigger button. */
  triggerClassName?: string
}

export function EventCombobox({
  value,
  onChange,
  ariaLabel = 'Event name',
  clearable = false,
  placeholder = 'Select event',
  className,
  triggerClassName,
}: EventComboboxProps) {
  const { names, isPending, error } = useEventCatalog()
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [activeIndex, setActiveIndex] = useState(0)
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listId = useId()

  // Always include the current value so a selection is never hidden, even if it
  // is older than the catalog window or the catalog failed to load.
  const options = useMemo(() => {
    const all = value && !names.includes(value) ? [value, ...names] : names
    const needle = filter.trim().toLowerCase()
    return needle ? all.filter((name) => name.toLowerCase().includes(needle)) : all
  }, [names, value, filter])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  useEffect(() => {
    if (!open) return
    setFilter('')
    setActiveIndex(0)
    const handle = window.setTimeout(() => inputRef.current?.focus(), 0)
    return () => window.clearTimeout(handle)
  }, [open])

  // Keep the highlight in range as the filtered list shrinks.
  useEffect(() => {
    setActiveIndex((index) => Math.min(index, Math.max(0, options.length - 1)))
  }, [options.length])

  const select = (name: string) => {
    onChange(name)
    setOpen(false)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActiveIndex((index) => Math.min(index + 1, options.length - 1))
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActiveIndex((index) => Math.max(index - 1, 0))
    } else if (event.key === 'Enter') {
      event.preventDefault()
      const choice = options[activeIndex]
      if (choice) select(choice)
    } else if (event.key === 'Escape') {
      event.preventDefault()
      setOpen(false)
    }
  }

  return (
    <div ref={rootRef} className={cn('relative', className)}>
      <div className="flex items-center">
        <button
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-label={ariaLabel}
          onClick={() => setOpen((prev) => !prev)}
          className={cn(
            'flex h-9 w-full items-center justify-between gap-2 rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
            triggerClassName,
          )}
        >
          <span className={cn('truncate font-mono text-xs', !value && 'text-muted-foreground')}>
            {value || placeholder}
          </span>
          <ChevronsUpDown className="h-4 w-4 shrink-0 text-muted-foreground" />
        </button>
        {clearable && value ? (
          <button
            type="button"
            aria-label={`Clear ${ariaLabel}`}
            onClick={() => onChange('')}
            className="ml-1 shrink-0 rounded-md p-1 text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="absolute z-50 mt-1 w-full min-w-[14rem] rounded-md border border-input bg-popover p-1 text-popover-foreground shadow-md">
          <input
            ref={inputRef}
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Filter events…"
            aria-label="Filter events"
            className="mb-1 h-8 w-full rounded-sm border border-input bg-transparent px-2 text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <ul id={listId} role="listbox" className="max-h-56 overflow-auto">
            {isPending ? (
              <li className="px-2 py-1.5 text-xs text-muted-foreground">Loading events…</li>
            ) : error ? (
              <li className="px-2 py-1.5 text-xs text-destructive">Couldn’t load events</li>
            ) : options.length === 0 ? (
              <li className="px-2 py-1.5 text-xs text-muted-foreground">No matching events</li>
            ) : (
              options.map((name, index) => (
                <li key={name} role="option" aria-selected={name === value}>
                  <button
                    type="button"
                    onClick={() => select(name)}
                    onMouseEnter={() => setActiveIndex(index)}
                    className={cn(
                      'flex w-full items-center justify-between gap-2 rounded-sm px-2 py-1.5 text-left font-mono text-xs',
                      index === activeIndex ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
                    )}
                  >
                    <span className="truncate">{name}</span>
                    {name === value ? <Check className="h-3.5 w-3.5 shrink-0" /> : null}
                  </button>
                </li>
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  )
}
