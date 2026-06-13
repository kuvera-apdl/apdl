// "Copy as curl" keeps the console honest: every panel can reproduce its exact
// API call for the terminal.
export interface CurlSpec {
  method: string
  url: string
  headers?: Record<string, string>
  body?: unknown
}

function singleQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`
}

export function toCurl(spec: CurlSpec): string {
  const lines = [`curl -X ${spec.method} ${singleQuote(spec.url)}`]
  for (const [name, value] of Object.entries(spec.headers ?? {})) {
    lines.push(`  -H ${singleQuote(`${name}: ${value}`)}`)
  }
  if (spec.body !== undefined) {
    lines.push(`  -d ${singleQuote(JSON.stringify(spec.body, null, 2))}`)
  }
  return lines.join(' \\\n')
}
