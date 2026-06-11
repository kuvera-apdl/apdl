// The SSE flag_update payload carries no actor, so the live provider cannot
// tell "changed elsewhere" from "changed by this console". Mutations register
// their flag key here; SSE toasts are suppressed for keys written locally
// within the window (cache invalidation still runs either way).
const writes = new Map<string, number>()
const WINDOW_MS = 5000

export function noteLocalWrite(key: string): void {
  writes.set(key, Date.now())
}

export function wasRecentlyWrittenLocally(key: string): boolean {
  const at = writes.get(key)
  return at !== undefined && Date.now() - at < WINDOW_MS
}
