// Client-local run history — the only run listing available until the
// agents service grows a runs-list endpoint (plan gap G1).
import type { AnalysisType } from '@/api/types/agents'

export interface TrackedRun {
  run_id: string
  triggered_at: string
  last_status: string
  autonomy_level: number
  analysis_types: AnalysisType[]
}

const LIMIT = 50

function storageKey(workspaceId: string): string {
  return `apdl-admin:agent-runs:${workspaceId}`
}

export function loadTrackedRuns(workspaceId: string): TrackedRun[] {
  try {
    const raw = localStorage.getItem(storageKey(workspaceId))
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? (parsed as TrackedRun[]) : []
  } catch {
    return []
  }
}

export function trackRun(workspaceId: string, run: TrackedRun): void {
  const runs = [run, ...loadTrackedRuns(workspaceId).filter((entry) => entry.run_id !== run.run_id)]
  localStorage.setItem(storageKey(workspaceId), JSON.stringify(runs.slice(0, LIMIT)))
}

export function updateTrackedRunStatus(workspaceId: string, runId: string, status: string): void {
  const runs = loadTrackedRuns(workspaceId)
  const index = runs.findIndex((entry) => entry.run_id === runId)
  if (index === -1 || runs[index]!.last_status === status) return
  runs[index] = { ...runs[index]!, last_status: status }
  localStorage.setItem(storageKey(workspaceId), JSON.stringify(runs))
}
