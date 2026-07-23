import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { assignWeightedVariant } from '../../src/flags/evaluator';
import { extractFlagConfig } from '../../src/flags/schema';
import {
  MAX_TOTAL_VARIANT_WEIGHT,
  MAX_VARIANTS,
  MAX_VARIANT_WEIGHT,
} from '../../src/flags/variant-contract';
import type { VariantConfig } from '../../src/flags/types';

interface ValidationCase {
  name: string;
  valid: boolean;
  default_variant: string;
  variants: VariantConfig[];
}

interface AssignmentCase {
  name: string;
  bucket: number;
  expected_variant: string;
  variants: VariantConfig[];
}

interface VariantVectors {
  limits: {
    max_variants: number;
    max_variant_weight: number;
    max_total_variant_weight: number;
  };
  validation_cases: ValidationCase[];
  assignment_cases: AssignmentCase[];
}

const vectors = JSON.parse(
  readFileSync(
    resolve(process.cwd(), '../../fixtures/gates/variant-weights.json'),
    'utf8'
  )
) as VariantVectors;

function flag(case_: ValidationCase): Record<string, unknown> {
  return {
    key: 'weight_contract',
    enabled: true,
    default_variant: case_.default_variant,
    variants: case_.variants,
    salt: 'salt',
    rules: [],
    fallthrough: {
      rollout: { percentage: 100, bucket_by: 'user_id' },
    },
    version: 1,
  };
}

describe('shared weighted-variant contract', () => {
  it('pins the exact cross-runtime limits', () => {
    expect(vectors.limits).toEqual({
      max_variants: MAX_VARIANTS,
      max_variant_weight: MAX_VARIANT_WEIGHT,
      max_total_variant_weight: MAX_TOTAL_VARIANT_WEIGHT,
    });
  });

  it.each(vectors.validation_cases)('$name', (case_) => {
    expect(extractFlagConfig(flag(case_)) !== null).toBe(case_.valid);
    if (!case_.valid) {
      expect(assignWeightedVariant(case_.variants, 50)).toBeNull();
    }
  });

  it.each(vectors.assignment_cases)('$name', (case_) => {
    expect(assignWeightedVariant(case_.variants, case_.bucket)).toBe(
      case_.expected_variant
    );
  });
});
