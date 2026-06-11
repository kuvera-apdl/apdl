import { useEffect, useState } from 'react'

import { formatDateTime, formatRelative } from '@/lib/format'

export function RelativeTime({ value, className }: { value: string | null; className?: string }) {
  const [now, setNow] = useState(() => new Date())

  useEffect(() => {
    const interval = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(interval)
  }, [])

  return (
    <span title={formatDateTime(value)} className={className}>
      {formatRelative(value, now)}
    </span>
  )
}
