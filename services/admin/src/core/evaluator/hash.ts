// FNV-1a 32-bit bucketing, byte-for-byte identical to the JS SDK
// (sdk/javascript/src/flags/hash.ts), the Python SDK, and the config service.
// Golden values: fixtures/gates/parity.json.
const UINT32_MAX = 0xffffffff
const FNV_OFFSET_BASIS = 2166136261
const FNV_PRIME = 16777619

export function hashBucket(flagKey: string, salt: string, unitId: string): number {
  let hash = FNV_OFFSET_BASIS
  const bytes = new TextEncoder().encode(`${flagKey}:${salt}:${unitId}`)

  for (const byte of bytes) {
    hash ^= byte
    hash = Math.imul(hash, FNV_PRIME) >>> 0
  }

  return hash >>> 0
}

export function percentageBucket(flagKey: string, salt: string, unitId: string): number {
  return (hashBucket(flagKey, salt, unitId) / UINT32_MAX) * 100.0
}
