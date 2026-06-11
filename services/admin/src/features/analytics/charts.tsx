// Recharts wrappers (AD-3). SVG attributes can't resolve CSS variables, so
// series colors are fixed hexes that read on both themes.
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

export const SERIES_COLORS = {
  events: '#0ea5e9',
  users: '#8b5cf6',
  accent: '#10b981',
}

const AXIS_STYLE = { fontSize: 11 }

interface TimeseriesChartProps {
  buckets: { bucket: string; event_count: number; unique_users: number }[]
  mode: 'line' | 'bar'
}

function shortBucket(value: string): string {
  // "2026-06-10T14:00:00" → "06-10 14:00" / "2026-06-10" → "06-10"
  const [datePart, timePart] = value.split('T')
  const shortDate = datePart?.slice(5) ?? value
  return timePart ? `${shortDate} ${timePart.slice(0, 5)}` : shortDate
}

export function TimeseriesChart({ buckets, mode }: TimeseriesChartProps) {
  const data = buckets.map((bucket) => ({ ...bucket, label: shortBucket(bucket.bucket) }))
  return (
    <ResponsiveContainer width="100%" height={280}>
      <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.25} />
        <XAxis dataKey="label" tick={AXIS_STYLE} minTickGap={24} />
        <YAxis tick={AXIS_STYLE} width={48} allowDecimals={false} />
        <Tooltip
          contentStyle={{
            background: 'hsl(var(--popover))',
            border: '1px solid hsl(var(--border))',
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        {mode === 'bar' ? (
          <Bar dataKey="event_count" name="events" fill={SERIES_COLORS.events} radius={[3, 3, 0, 0]} />
        ) : (
          <Line
            type="monotone"
            dataKey="event_count"
            name="events"
            stroke={SERIES_COLORS.events}
            dot={false}
            strokeWidth={2}
          />
        )}
        <Line
          type="monotone"
          dataKey="unique_users"
          name="unique users"
          stroke={SERIES_COLORS.users}
          dot={false}
          strokeWidth={2}
        />
      </ComposedChart>
    </ResponsiveContainer>
  )
}

interface SimpleLineChartProps {
  data: { label: string; value: number }[]
  height?: number
  color?: string
  unit?: string
}

export function SimpleLineChart({ data, height = 200, color = SERIES_COLORS.accent, unit }: SimpleLineChartProps) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.25} />
        <XAxis dataKey="label" tick={AXIS_STYLE} minTickGap={24} />
        <YAxis tick={AXIS_STYLE} width={44} unit={unit} />
        <Tooltip
          contentStyle={{
            background: 'hsl(var(--popover))',
            border: '1px solid hsl(var(--border))',
            borderRadius: 8,
            fontSize: 12,
          }}
        />
        <Line type="monotone" dataKey="value" stroke={color} dot={false} strokeWidth={2} />
      </LineChart>
    </ResponsiveContainer>
  )
}
