import { readdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { gzipSync } from 'node:zlib'

const KIB = 1024
const DIST_DIR = fileURLToPath(new URL('../dist', import.meta.url))

const BUDGETS = Object.freeze({
  initialJavaScript: {
    rawBytes: 525 * KIB,
    gzipBytes: 165 * KIB,
  },
  deferredChunk: {
    rawBytes: 450 * KIB,
    gzipBytes: 120 * KIB,
  },
  totalJavaScript: {
    rawBytes: 1536 * KIB,
    gzipBytes: 450 * KIB,
  },
})

function readAttribute(tag, attribute) {
  const match = tag.match(new RegExp(`\\b${attribute}=(["'])(.*?)\\1`, 'i'))
  return match?.[2] ?? null
}

function localJavaScriptPath(reference) {
  const url = new URL(reference, 'https://bundle.apdl.invalid/')
  if (url.origin !== 'https://bundle.apdl.invalid') {
    throw new Error(`External JavaScript is not allowed in the production entrypoint: ${reference}`)
  }

  const decodedPath = decodeURIComponent(url.pathname).replace(/^\/+/, '')
  const normalizedPath = path.posix.normalize(decodedPath)
  if (
    normalizedPath === '..' ||
    normalizedPath.startsWith('../') ||
    !normalizedPath.endsWith('.js')
  ) {
    throw new Error(`Invalid JavaScript asset path in production entrypoint: ${reference}`)
  }
  return normalizedPath
}

function initialJavaScriptPaths(html) {
  const paths = new Set()

  for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) {
    const source = readAttribute(tag, 'src')
    if (source) paths.add(localJavaScriptPath(source))
  }

  for (const tag of html.match(/<link\b[^>]*>/gi) ?? []) {
    const rel = readAttribute(tag, 'rel')
    const href = readAttribute(tag, 'href')
    if (rel?.split(/\s+/).includes('modulepreload') && href) {
      paths.add(localJavaScriptPath(href))
    }
  }

  if (paths.size === 0) {
    throw new Error('No initial JavaScript assets were found in dist/index.html')
  }
  return paths
}

async function listJavaScriptFiles(directory, prefix = '') {
  const files = []
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const relativePath = path.posix.join(prefix, entry.name)
    if (entry.isDirectory()) {
      files.push(...(await listJavaScriptFiles(path.join(directory, entry.name), relativePath)))
    } else if (entry.isFile() && entry.name.endsWith('.js')) {
      files.push(relativePath)
    }
  }
  return files.sort()
}

async function measure(relativePath) {
  const contents = await readFile(path.join(DIST_DIR, relativePath))
  return {
    path: relativePath,
    rawBytes: contents.byteLength,
    gzipBytes: gzipSync(contents).byteLength,
  }
}

function sumMeasurements(measurements) {
  return measurements.reduce(
    (total, measurement) => ({
      rawBytes: total.rawBytes + measurement.rawBytes,
      gzipBytes: total.gzipBytes + measurement.gzipBytes,
    }),
    { rawBytes: 0, gzipBytes: 0 },
  )
}

function formatKib(bytes) {
  return `${(bytes / KIB).toFixed(2)} KiB`
}

function recordBudget(violations, label, actualBytes, budgetBytes) {
  if (actualBytes > budgetBytes) {
    violations.push(
      `${label}: ${formatKib(actualBytes)} exceeds ${formatKib(budgetBytes)}`,
    )
  }
}

const html = await readFile(path.join(DIST_DIR, 'index.html'), 'utf8')
const initialPaths = initialJavaScriptPaths(html)
const allPaths = await listJavaScriptFiles(DIST_DIR)
const missingInitialPaths = [...initialPaths].filter((assetPath) => !allPaths.includes(assetPath))
if (missingInitialPaths.length > 0) {
  throw new Error(`Missing initial JavaScript assets: ${missingInitialPaths.join(', ')}`)
}

const measurements = await Promise.all(allPaths.map(measure))
const initialMeasurements = measurements.filter((measurement) =>
  initialPaths.has(measurement.path),
)
const deferredMeasurements = measurements.filter(
  (measurement) => !initialPaths.has(measurement.path),
)
if (deferredMeasurements.length === 0) {
  throw new Error('No deferred JavaScript chunks were emitted; route splitting is not active')
}

const initialTotal = sumMeasurements(initialMeasurements)
const total = sumMeasurements(measurements)
const largestDeferredRaw = deferredMeasurements.reduce((largest, current) =>
  current.rawBytes > largest.rawBytes ? current : largest,
)
const largestDeferredGzip = deferredMeasurements.reduce((largest, current) =>
  current.gzipBytes > largest.gzipBytes ? current : largest,
)

console.table([
  {
    metric: 'Initial JavaScript',
    raw: formatKib(initialTotal.rawBytes),
    gzip: formatKib(initialTotal.gzipBytes),
    budget: `${formatKib(BUDGETS.initialJavaScript.rawBytes)} raw / ${formatKib(BUDGETS.initialJavaScript.gzipBytes)} gzip`,
  },
  {
    metric: `Largest deferred raw (${largestDeferredRaw.path})`,
    raw: formatKib(largestDeferredRaw.rawBytes),
    gzip: formatKib(largestDeferredRaw.gzipBytes),
    budget: `${formatKib(BUDGETS.deferredChunk.rawBytes)} raw`,
  },
  {
    metric: `Largest deferred gzip (${largestDeferredGzip.path})`,
    raw: formatKib(largestDeferredGzip.rawBytes),
    gzip: formatKib(largestDeferredGzip.gzipBytes),
    budget: `${formatKib(BUDGETS.deferredChunk.gzipBytes)} gzip`,
  },
  {
    metric: 'Total JavaScript',
    raw: formatKib(total.rawBytes),
    gzip: formatKib(total.gzipBytes),
    budget: `${formatKib(BUDGETS.totalJavaScript.rawBytes)} raw / ${formatKib(BUDGETS.totalJavaScript.gzipBytes)} gzip`,
  },
])

const violations = []
recordBudget(
  violations,
  'Initial JavaScript raw size',
  initialTotal.rawBytes,
  BUDGETS.initialJavaScript.rawBytes,
)
recordBudget(
  violations,
  'Initial JavaScript gzip size',
  initialTotal.gzipBytes,
  BUDGETS.initialJavaScript.gzipBytes,
)
for (const measurement of deferredMeasurements) {
  recordBudget(
    violations,
    `${measurement.path} raw size`,
    measurement.rawBytes,
    BUDGETS.deferredChunk.rawBytes,
  )
  recordBudget(
    violations,
    `${measurement.path} gzip size`,
    measurement.gzipBytes,
    BUDGETS.deferredChunk.gzipBytes,
  )
}
recordBudget(
  violations,
  'Total JavaScript raw size',
  total.rawBytes,
  BUDGETS.totalJavaScript.rawBytes,
)
recordBudget(
  violations,
  'Total JavaScript gzip size',
  total.gzipBytes,
  BUDGETS.totalJavaScript.gzipBytes,
)

if (violations.length > 0) {
  console.error(`Bundle size budget failed:\n- ${violations.join('\n- ')}`)
  process.exitCode = 1
} else {
  console.log('Bundle size budget passed.')
}
