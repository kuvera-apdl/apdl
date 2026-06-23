// The timeseries endpoint only returns buckets that actually had events, so
// charts show gaps. densifyBuckets fills every slot across the requested date
// range with a zeroed bucket where the API returned none — 24 hourly slots per
// day for 'hour', one per day for 'day'.
//
// Timezone-safe: the grid spans the same calendar date range the server filtered
// on (event_date BETWEEN start_date AND end_date), and bucket keys derive from
// the server's own `YYYY-MM-DD[THH:00:00]` strings — so a real bucket's key is
// always generated and never dropped, regardless of server vs. client tz.

export interface TimeBucket {
  bucket: string
  event_count: number
  unique_users: number
}

export type BucketGranularity = 'hour' | 'day'

const pad = (value: number): string => String(value).padStart(2, '0')

// Daily buckets come back at midnight (`...T00:00:00`); key them by date alone
// so the chart label reads "06-22" rather than "06-22 00:00".
const keyOf = (bucket: string, granularity: BucketGranularity): string =>
  granularity === 'day' ? bucket.slice(0, 10) : bucket

/**
 * Return one bucket per slot for every day in [startDate, endDate] (inclusive,
 * `YYYY-MM-DD`), preserving the API's counts and zero-filling the rest.
 */
export function densifyBuckets(
  buckets: readonly TimeBucket[],
  startDate: string,
  endDate: string,
  granularity: BucketGranularity,
): TimeBucket[] {
  const byKey = new Map(buckets.map((bucket) => [keyOf(bucket.bucket, granularity), bucket]))
  const out: TimeBucket[] = []
  const seen = new Set<string>()

  const push = (key: string) => {
    const existing = byKey.get(key)
    out.push({
      bucket: key,
      event_count: existing?.event_count ?? 0,
      unique_users: existing?.unique_users ?? 0,
    })
    seen.add(key)
  }

  // Iterate days in UTC to avoid DST quirks; only Y-M-D is used to build keys.
  const start = new Date(`${startDate}T00:00:00Z`)
  const end = new Date(`${endDate}T00:00:00Z`)
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return buckets.map((bucket) => ({
      bucket: keyOf(bucket.bucket, granularity),
      event_count: bucket.event_count,
      unique_users: bucket.unique_users,
    }))
  }

  for (const day = new Date(start); day <= end; day.setUTCDate(day.getUTCDate() + 1)) {
    const ymd = `${day.getUTCFullYear()}-${pad(day.getUTCMonth() + 1)}-${pad(day.getUTCDate())}`
    if (granularity === 'hour') {
      for (let hour = 0; hour < 24; hour++) push(`${ymd}T${pad(hour)}:00:00`)
    } else {
      push(ymd)
    }
  }

  // Safety net: keep any real bucket that fell outside the generated grid.
  for (const bucket of buckets) {
    const key = keyOf(bucket.bucket, granularity)
    if (!seen.has(key)) {
      out.push({ bucket: key, event_count: bucket.event_count, unique_users: bucket.unique_users })
    }
  }

  out.sort((a, b) => (a.bucket < b.bucket ? -1 : a.bucket > b.bucket ? 1 : 0))
  return out
}

/**
 * The `hours` most recent whole UTC hours, oldest→newest, each carrying the
 * matching API count (zeroed where none). Powers the "today" view as a rolling
 * window that ends at the current UTC hour, rather than the local calendar day.
 *
 * The API buckets in UTC and returns keys like `YYYY-MM-DDTHH:00:00`, so the
 * generated slot keys line up exactly with returned buckets. Buckets outside the
 * window are dropped (the caller fetches whole UTC dates, which can overhang).
 */
export function rollingHourBuckets(buckets: readonly TimeBucket[], hours: number): TimeBucket[] {
  const byKey = new Map(buckets.map((bucket) => [bucket.bucket, bucket]))
  const now = new Date()
  const currentHourMs = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate(),
    now.getUTCHours(),
  )
  const out: TimeBucket[] = []
  for (let i = hours - 1; i >= 0; i--) {
    const slot = new Date(currentHourMs - i * 3_600_000)
    const key = `${slot.getUTCFullYear()}-${pad(slot.getUTCMonth() + 1)}-${pad(slot.getUTCDate())}T${pad(slot.getUTCHours())}:00:00`
    const existing = byKey.get(key)
    out.push({
      bucket: key,
      event_count: existing?.event_count ?? 0,
      unique_users: existing?.unique_users ?? 0,
    })
  }
  return out
}
