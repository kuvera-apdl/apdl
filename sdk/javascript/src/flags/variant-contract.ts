/** Strict bounds shared by every APDL weighted-variant runtime. */

export const MAX_VARIANTS = 10;
export const MAX_VARIANT_WEIGHT = 9_007_199_254_740_991;
export const MAX_TOTAL_VARIANT_WEIGHT = 9_007_199_254_740_991;

interface WeightedValue {
  weight?: unknown;
}

export function hasCanonicalVariantWeights(
  variants: readonly WeightedValue[]
): boolean {
  if (variants.length === 0 || variants.length > MAX_VARIANTS) {
    return false;
  }

  let totalWeight = 0;
  for (const variant of variants) {
    const weight = variant.weight;
    if (
      typeof weight !== 'number'
      || !Number.isSafeInteger(weight)
      || weight < 0
      || weight > MAX_VARIANT_WEIGHT
      || totalWeight > MAX_TOTAL_VARIANT_WEIGHT - weight
    ) {
      return false;
    }
    totalWeight += weight;
  }

  return totalWeight > 0;
}
