import type { VariantConfig } from '@/api/types/flags'

/**
 * Parse server timestamps. The API emits both `datetime.isoformat()`
 * ("2026-06-10T12:00:00+00:00") and `str(datetime)` with a space separator
 * ("2026-06-10 12:00:00.123+00:00", used by audit entries).
 */
export function parseServerDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

const RELATIVE_UNITS: [number, string][] = [
  [365 * 24 * 60 * 60 * 1000, 'y'],
  [30 * 24 * 60 * 60 * 1000, 'mo'],
  [24 * 60 * 60 * 1000, 'd'],
  [60 * 60 * 1000, 'h'],
  [60 * 1000, 'm'],
  [1000, 's'],
]

export function formatRelative(value: string | null | undefined, now: Date = new Date()): string {
  const date = parseServerDate(value)
  if (!date) return '—'
  const diff = now.getTime() - date.getTime()
  const abs = Math.abs(diff)
  if (abs < 10_000) return 'just now'
  for (const [unitMs, suffix] of RELATIVE_UNITS) {
    if (abs >= unitMs) {
      const amount = Math.floor(abs / unitMs)
      return diff >= 0 ? `${amount}${suffix} ago` : `in ${amount}${suffix}`
    }
  }
  return 'just now'
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseServerDate(value)
  return date ? date.toLocaleString() : '—'
}

/** Plain YYYY-MM-DD comparison, matching the server's `review_by < today`. */
export function isPastDate(value: string | null | undefined, today: Date = new Date()): boolean {
  if (!value) return false
  const todayIso = today.toISOString().slice(0, 10)
  return value < todayIso
}

export function formatPercent(value: number): string {
  const rounded = Math.round(value * 10) / 10
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(1)}%`
}

export function formatMs(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.round(ms)} ms`
}

/** "control/treatment 50:50" — variant keys with their normalized weight split. */
export function variantSummary(variants: VariantConfig[]): string {
  if (variants.length === 0) return '—'
  const keys = variants.map((variant) => variant.key).join('/')
  const total = variants.reduce((sum, variant) => sum + variant.weight, 0)
  if (total <= 0) return keys
  const shares = variants.map((variant) => Math.round((variant.weight / total) * 100)).join(':')
  return `${keys} ${shares}`
}

export function variantShare(variant: VariantConfig, variants: VariantConfig[]): number {
  const total = variants.reduce((sum, entry) => sum + entry.weight, 0)
  return total > 0 ? (variant.weight / total) * 100 : 0
}

export function initials(name: string): string {
  const base = name.split('@')[0] ?? name
  const parts = base.split(/[\s._-]+/).filter(Boolean)
  if (parts.length === 0) return '?'
  return parts
    .slice(0, 2)
    .map((part) => (part[0] ?? '').toUpperCase())
    .join('')
}
