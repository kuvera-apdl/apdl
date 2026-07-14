// The one status vocabulary for the loop (admin-console-purpose-ia.md).
//
// Every loop surface — Decide, Watch, Learn, Ship, the experiment thread hub,
// Overview — speaks these stages, mapped ONCE here from the various backend
// states (run phase/status and experiment status). Pages never re-derive
// colors; they call loopStatusMeta / <LoopStatusPill>.

export type LoopStage =
  | 'designing'
  | 'awaiting_approval'
  | 'building'
  | 'running'
  | 'needs_data'
  | 'ship'
  | 'rollback'
  | 'iterate'
  | 'extend'
  | 'done'
  | 'failed'

export type LoopTone = 'neutral' | 'info' | 'warn' | 'success' | 'danger' | 'accent'

interface StageMeta {
  label: string
  tone: LoopTone
}

const STAGE_META: Record<LoopStage, StageMeta> = {
  designing: { label: 'designing', tone: 'neutral' },
  awaiting_approval: { label: 'awaiting approval', tone: 'warn' },
  building: { label: 'building', tone: 'info' },
  running: { label: 'running', tone: 'info' },
  needs_data: { label: 'needs more data', tone: 'warn' },
  ship: { label: 'ship', tone: 'success' },
  rollback: { label: 'rollback', tone: 'danger' },
  iterate: { label: 'iterate', tone: 'accent' },
  extend: { label: 'extend', tone: 'info' },
  done: { label: 'done', tone: 'success' },
  failed: { label: 'failed', tone: 'danger' },
}

// Tailwind classes per tone, matching the existing pill palette (StatePill /
// RunStatusPill) so the new vocabulary is visually continuous with the old.
export const LOOP_TONE_CLASSES: Record<LoopTone, string> = {
  neutral:
    'border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300',
  info: 'border-sky-200 bg-sky-100 text-sky-800 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-300',
  warn: 'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
  success:
    'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  danger: 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300',
  accent:
    'border-violet-200 bg-violet-100 text-violet-800 dark:border-violet-900 dark:bg-violet-950/60 dark:text-violet-300',
}

export function loopStageMeta(stage: LoopStage): StageMeta {
  return STAGE_META[stage]
}

// Backend run status/phase → loop stage. The supervisor's phase carries the
// gated-agent name (e.g. "experiment_design_approval"); status carries the
// lifecycle. Kept total so an unknown value degrades to a neutral 'designing'
// rather than throwing on a surface.
export function runToLoopStage(status: string, phase = ''): LoopStage {
  if (status === 'waiting_approval') return 'awaiting_approval'
  if (status === 'failed' || status === 'completed_with_errors') return 'failed'
  if (status === 'completed' || status === 'approved') return 'done'
  if (status === 'rejected') return 'done'
  if (status === 'running' || status === 'started') {
    if (phase.startsWith('code_implementation')) return 'building'
    return 'designing'
  }
  return 'designing'
}
