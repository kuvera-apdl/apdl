// Canonical fixtures matching serialize_flag() / the audit store exactly —
// these are what the live API returns (Strict Schema Rule test material).
import type { FlagAuditEntry, FlagConfig } from '../../src/api/types/flags'
import type {
  ReviewVerdict,
  VerificationCoverage,
  VerificationPlan,
} from '../../src/api/types/codegen'
import type { Workspace } from '../../src/core/workspace'

export function makeVerificationPlan(
  overrides: Partial<VerificationPlan> = {},
): VerificationPlan {
  return {
    schema_version: 'verification_plan@1',
    source_ledger_sha256: 'a'.repeat(64),
    repo_profile_schema_version: 'repo_profile@1',
    risk: 'high',
    authority: 'github_ci',
    apdl_local_execution_authoritative: false,
    workflow_gate_policy: 'preserve_or_strengthen',
    test_runner_configured: true,
    test_commands: [
      {
        command: 'npm test',
        cwd: '.',
        source_path: 'package.json',
      },
    ],
    github_workflow_paths: ['.github/workflows/ci.yml'],
    protected_workflow_paths: ['.github/workflows/ci.yml'],
    disposition: 'github_ci_planned',
    disposition_reason: 'Repository tests are configured in the protected GitHub CI workflow.',
    items: [
      {
        plan_item_id: 'VP-001',
        requirement_id: 'REQ-001',
        surface: 'api',
        policy_check: 'strict_request_response_schema',
        requirement_risk: 'high',
        expected_assertion: 'Reject payloads with unknown fields.',
        expected_ci_evidence_ids: ['CI-001'],
        requires_changed_test_for_pr: true,
        disposition: 'required_in_github_ci',
      },
    ],
    ...overrides,
  }
}

export function makeVerificationCoverage(
  overrides: Partial<VerificationCoverage> = {},
): VerificationCoverage {
  return {
    schema_version: 'verification_coverage@1',
    source_ledger_sha256: 'a'.repeat(64),
    authority: 'github_ci',
    github_has_not_reported: true,
    apdl_declared_verified: false,
    workflow_gate_policy: 'preserve_or_strengthen',
    disposition: 'ready_for_github_ci',
    disposition_reason: 'The diff includes the required regression test path for GitHub CI.',
    changed_test_paths: ['src/api/__tests__/schema.test.ts'],
    changed_workflow_paths: [],
    changed_protected_workflow_paths: [],
    relaxed_workflow_paths: [],
    items: [
      {
        plan_item_id: 'VP-001',
        status: 'coverage_path_present',
        coverage_paths: ['src/api/__tests__/schema.test.ts'],
      },
    ],
    ...overrides,
  }
}

export function makeReviewVerdict(
  overrides: Partial<ReviewVerdict> = {},
): ReviewVerdict {
  return {
    schema_version: 'review_verdict@1',
    reviewed_diff_sha256: 'b'.repeat(64),
    overall_decision: 'rejected',
    model_response_status: 'parsed',
    deterministic_errors_override_model: true,
    requirement_decisions: [
      {
        requirement_id: 'REQ-001',
        decision: 'rejected',
        evidence_ids: ['DIFF:src/api/schema.ts'],
        rationale: 'The new resource is initialized without a corresponding cleanup path.',
        actionable_instructions: ['Add cleanup for the initialized resource.'],
      },
    ],
    deterministic_findings: [
      {
        finding_id: 'RF-001',
        code: 'missing_cleanup',
        severity: 'error',
        requirement_ids: ['REQ-001'],
        evidence_ids: ['DIFF:src/api/schema.ts'],
        message: 'The generated diff initializes a resource but does not release it.',
        actionable_instruction: 'Add cleanup for the initialized resource.',
      },
    ],
    uncertainties: [],
    actionable_instructions: ['Add cleanup for the initialized resource.'],
    ...overrides,
  }
}

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
