// Canonical fixtures matching serialize_flag() / the audit store exactly —
// these are what the live API returns (Strict Schema Rule test material).
import type { FlagAuditEntry, FlagConfig } from '../../src/api/types/flags'
import type { Workspace } from '../../src/core/workspace'

export function makeFlag(overrides: Partial<FlagConfig> = {}): FlagConfig {
  return {
    key: 'checkout-cta',
    project_id: 'demo',
    name: 'Checkout CTA experiment',
    state: 'active',
    owners: ['kirill'],
    review_by: '2027-01-01',
    description: 'Tests the new checkout call-to-action',
    enabled: true,
    default_variant: 'control',
    variants: [
      { key: 'control', weight: 1 },
      { key: 'treatment', weight: 1 },
    ],
    rules: [
      {
        id: 'rule_beta',
        name: 'beta users',
        conditions: [
          { attribute: 'plan', operator: 'equals', value: 'pro' },
          { attribute: 'beta_opt_in', operator: 'exists', value: null },
        ],
        rollout: { percentage: 50, bucket_by: 'user_id' },
      },
    ],
    fallthrough: { rollout: { percentage: 10, bucket_by: 'user_id' } },
    salt: 'fixture-salt',
    evaluation_mode: 'client',
    auto_disable: true,
    guardrails: [
      {
        metric: 'frontend_error_rate',
        threshold: '2x_baseline',
        scope: '',
        minimum_exposures: 100,
        window_minutes: 10,
      },
    ],
    disabled_reason: '',
    disabled_by: '',
    disabled_at: null,
    version: 3,
    created_at: '2026-06-01T10:00:00+00:00',
    updated_at: '2026-06-09T15:30:00+00:00',
    archived_at: null,
    ...overrides,
  }
}

export function makeAuditEntry(overrides: Partial<FlagAuditEntry> = {}): FlagAuditEntry {
  return {
    id: 42,
    project_id: 'demo',
    flag_key: 'checkout-cta',
    action: 'flag_updated',
    actor: 'kirill',
    previous_version: 2,
    new_version: 3,
    before: { name: 'Old name', version: 2 },
    after: { name: 'Checkout CTA experiment', version: 3 },
    evidence: {},
    reason: null,
    // Audit timestamps come from str(datetime) — space separator.
    created_at: '2026-06-09 15:30:00.123456+00:00',
    ...overrides,
  }
}

export function makeWorkspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'demo',
    name: 'demo',
    projectId: 'demo',
    actor: 'tester@example.com',
    ...overrides,
  }
}

export function seedWorkspace(workspace: Workspace = makeWorkspace()): Workspace {
  localStorage.setItem('apdl-admin:active-project', workspace.id)
  return workspace
}
