const UINT32_MAX = 0xffffffff;
const FNV_OFFSET_BASIS = 2166136261;
const FNV_PRIME = 16777619;

/**
 * FNV-1a 32-bit hash matching services/config/app/flags/evaluator.py.
 */
export function hashBucket(key: string, userId: string): number {
  let hash = FNV_OFFSET_BASIS;
  const bytes = utf8Bytes(`${key}:${userId}`);

  for (const byte of bytes) {
    hash ^= byte;
    hash = Math.imul(hash, FNV_PRIME) >>> 0;
  }

  return hash >>> 0;
}

export function percentageBucket(key: string, userId: string): number {
  return (hashBucket(key, userId) / UINT32_MAX) * 100.0;
}

export function isInRollout(
  flagKey: string,
  userId: string,
  percentage: number
): boolean {
  if (percentage >= 100.0) return true;
  if (percentage <= 0.0) return false;
  return percentageBucket(flagKey, userId) < percentage;
}

function utf8Bytes(input: string): Uint8Array {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(input);
  }

  const bytes: number[] = [];
  for (let i = 0; i < input.length; i++) {
    let codePoint = input.charCodeAt(i);

    if (codePoint >= 0xd800 && codePoint <= 0xdbff && i + 1 < input.length) {
      const next = input.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        codePoint = 0x10000 + ((codePoint - 0xd800) << 10) + (next - 0xdc00);
        i++;
      }
    }

    if (codePoint < 0x80) {
      bytes.push(codePoint);
    } else if (codePoint < 0x800) {
      bytes.push(0xc0 | (codePoint >> 6));
      bytes.push(0x80 | (codePoint & 0x3f));
    } else if (codePoint < 0x10000) {
      bytes.push(0xe0 | (codePoint >> 12));
      bytes.push(0x80 | ((codePoint >> 6) & 0x3f));
      bytes.push(0x80 | (codePoint & 0x3f));
    } else {
      bytes.push(0xf0 | (codePoint >> 18));
      bytes.push(0x80 | ((codePoint >> 12) & 0x3f));
      bytes.push(0x80 | ((codePoint >> 6) & 0x3f));
      bytes.push(0x80 | (codePoint & 0x3f));
    }
  }

  return new Uint8Array(bytes);
}
