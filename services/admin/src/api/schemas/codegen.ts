// Codegen-service mirrors (services/codegen/app/models/changeset.py). The
// changeset status is a plain string on the wire; the UI maps known values.
import { z } from 'zod'

// One LLM prompt the run actually sent (brief compilation, an edit instruction
// handed to the coding agent, or a pre-push diff review). `system` is null for
// the edit stage: Aider supplies its own built-in system prompt there.
export const changesetPromptSchema = z
  .object({
    stage: z.string(),
    label: z.string(),
    system: z.string().nullable(),
    user: z.string(),
    notes: z.string().nullable(),
  })
  .strict()

export const taskSpecSchema = z
  .object({
    title: z.string(),
    spec: z.string(),
    context: z.record(z.unknown()),
    constraints: z.array(z.string()),
  })
  .strict()

const runtimeFingerprintSchema = z
  .object({
    schema_version: z.literal('runtime_fingerprint@1'),
    runtime_name: z.string(),
    runtime_version: z.string(),
    operating_system: z.string(),
    architecture: z.string(),
  })
  .strict()

const contractRequestSchema = z
  .object({
    schema_version: z.literal('contract_request@1'),
    requirement_ids: z.array(z.string()),
    ecosystem: z.string(),
    package_path: z.string(),
    package_name: z.string(),
    exact_version: z.string().nullable(),
    manifest_path: z.string(),
    lockfile_path: z.string().nullable(),
    symbols: z.array(z.string()),
  })
  .strict()

const contractCacheIdentitySchema = z
  .object({
    schema_version: z.literal('contract_cache_identity@1'),
    project_scope: z.string(),
    repository: z.string(),
    ecosystem: z.string(),
    package_path: z.string(),
    manifest_path: z.string(),
    manifest_sha256: z.string(),
    lockfile_path: z.string(),
    lockfile_sha256: z.string(),
    runtime: runtimeFingerprintSchema,
    extractor_version: z.string(),
    selection_sha256: z.string(),
    cache_key: z.string(),
  })
  .strict()

const sourceProvenanceSchema = z
  .object({
    schema_version: z.literal('contract_provenance@1'),
    manifest_path: z.string(),
    manifest_sha256: z.string(),
    lockfile_path: z.string(),
    lockfile_sha256: z.string(),
    installed_root: z.string(),
    runtime: runtimeFingerprintSchema,
  })
  .strict()

const contractSourceSchema = z
  .object({
    schema_version: z.literal('contract_source@1'),
    source_id: z.string(),
    kind: z.enum([
      'installed_metadata',
      'installed_types',
      'installed_exports',
      'bundled_documentation',
      'installed_implementation',
    ]),
    relative_path: z.string(),
    sha256: z.string(),
    provenance: sourceProvenanceSchema,
  })
  .strict()

const contractSymbolSchema = z
  .object({
    schema_version: z.literal('contract_symbol@1'),
    qualified_name: z.string(),
    kind: z.enum([
      'function',
      'async_function',
      'class',
      'interface',
      'type_alias',
      'constant',
      'module_export',
      'method',
    ]),
    signature: z.string(),
    source_ids: z.array(z.string()),
  })
  .strict()

const lifecycleFactSchema = z
  .object({
    schema_version: z.literal('lifecycle_fact@1'),
    kind: z.enum(['initialization', 'readiness', 'asynchronous', 'singleton', 'cleanup', 'error']),
    statement: z.string(),
    source_ids: z.array(z.string()),
  })
  .strict()

const compileCheckedExampleSchema = z
  .object({
    schema_version: z.literal('compile_checked_example@1'),
    language: z.string(),
    snippet: z.string(),
    command: z.string(),
    tool_version: z.string(),
    output_sha256: z.string(),
    source_ids: z.array(z.string()),
    check_result: z.literal('passed'),
  })
  .strict()

const contractEvidenceSchema = z
  .object({
    schema_version: z.literal('contract_evidence@1'),
    contract_id: z.string(),
    ecosystem: z.string(),
    package_path: z.string(),
    package_name: z.string(),
    exact_version: z.string(),
    sources: z.array(contractSourceSchema),
    symbols: z.array(contractSymbolSchema),
    lifecycle_facts: z.array(lifecycleFactSchema),
    examples: z.array(compileCheckedExampleSchema),
  })
  .strict()

const contractBlockerSchema = z
  .object({
    schema_version: z.literal('contract_blocker@1'),
    code: z.enum([
      'missing_manifest',
      'missing_lockfile',
      'conflicting_lockfiles',
      'unresolved_version',
      'version_mismatch',
      'unsupported_ecosystem',
      'unsupported_toolchain',
      'install_failed',
      'package_not_found',
      'inspection_failed',
      'compile_check_unavailable',
      'example_check_failed',
      'budget_exceeded',
    ]),
    severity: z.enum(['warning', 'blocking']),
    package_name: z.string(),
    message: z.string(),
    paths: z.array(z.string()),
  })
  .strict()

export const contractBundleSchema = z
  .object({
    schema_version: z.literal('contract_bundle@1'),
    resolutions: z.array(
      z
        .object({
          schema_version: z.literal('contract_resolution@1'),
          request: contractRequestSchema,
          cache_identity: contractCacheIdentitySchema.nullable(),
          disposition: z.enum(['ready', 'blocked']),
          evidence: contractEvidenceSchema.nullable(),
          blockers: z.array(contractBlockerSchema),
        })
        .strict(),
    ),
  })
  .strict()

const expectedCiEvidenceSchema = z.discriminatedUnion('kind', [
  z
    .object({
      kind: z.literal('github_check'),
      evidence_id: z.string(),
      check_name: z.string(),
      assertion: z.string(),
    })
    .strict(),
  z
    .object({
      kind: z.literal('repository_command'),
      evidence_id: z.string(),
      command: z.string(),
      cwd: z.string(),
      assertion: z.string(),
    })
    .strict(),
  z
    .object({
      kind: z.literal('observable_assertion'),
      evidence_id: z.string(),
      assertion: z.string(),
    })
    .strict(),
])

const requirementSchema = z
  .object({
    requirement_id: z.string(),
    source_kind: z.enum(['task_spec', 'acceptance_criterion', 'constraint']),
    original_source_text: z.string(),
    observable_behavior: z.string(),
    implementable_scope: z.string(),
    likely_targets: z.array(
      z.object({ path: z.string(), symbol: z.string().nullable() }).strict(),
    ),
    required_contract_evidence_ids: z.array(z.string()),
    expected_ci_evidence: z.array(expectedCiEvidenceSchema),
    risk: z.enum(['low', 'medium', 'high']),
    implementation_status: z.enum([
      'planned',
      'implemented',
      'confirmed_existing',
      'blocked',
      'descoped',
    ]),
    implementation_evidence: z.array(
      z
        .object({
          kind: z.enum(['changed', 'existing']),
          path: z.string(),
          symbol: z.string().nullable(),
          description: z.string(),
        })
        .strict(),
    ),
    decision_reason: z.string().nullable(),
  })
  .strict()

export const requirementLedgerSchema = z
  .object({
    schema_version: z.literal('requirement_ledger@1'),
    title: z.string(),
    source_sha256: z.string(),
    requirements: z.array(requirementSchema),
  })
  .strict()

const inspectionEvidenceSchema = z
  .object({
    evidence_id: z.string(),
    kind: z.enum([
      'file',
      'search',
      'symbol',
      'local_import',
      'caller',
      'route',
      'link',
      'test',
      'lockfile',
      'contract',
      'config',
    ]),
    path: z.string(),
    content_sha256: z.string(),
    start_line: z.number().int().nullable(),
    end_line: z.number().int().nullable(),
    source_path: z.string().nullable(),
    source_line: z.number().int().nullable(),
    target_path: z.string().nullable(),
    symbol: z.string().nullable(),
    excerpt: z.string().nullable(),
    truncated: z.boolean(),
  })
  .strict()

export const inspectionSnapshotSchema = z
  .object({
    schema_version: z.literal('inspection_snapshot@1'),
    root_label: z.literal('.'),
    evidence: z.array(inspectionEvidenceSchema),
    skipped_paths: z.array(z.string()),
    bytes_inspected: z.number().int().nonnegative(),
    truncated: z.boolean(),
  })
  .strict()

export const dependencySliceSchema = z
  .object({
    schema_version: z.literal('dependency_slice@1'),
    changed_files: z.array(inspectionEvidenceSchema),
    imported_local_symbols: z.array(inspectionEvidenceSchema),
    callers: z.array(inspectionEvidenceSchema),
    routes_and_handlers: z.array(inspectionEvidenceSchema),
    affected_tests: z.array(inspectionEvidenceSchema),
    relevant_lockfiles: z.array(inspectionEvidenceSchema),
    external_contracts: z.array(inspectionEvidenceSchema),
    unresolved_references: z.array(z.string()),
    truncated: z.boolean(),
  })
  .strict()

const verificationSurfaceSchema = z.enum([
  'general',
  'ui',
  'api',
  'sdk',
  'analytics',
  'database',
  'security',
  'billing',
  'concurrency',
])

const verificationCheckSchema = z.enum([
  'regression',
  'render',
  'interaction',
  'accessibility_smoke',
  'responsive_browser',
  'route_existence',
  'strict_request_response_schema',
  'error_cases',
  'exact_version_contract',
  'lifecycle',
  'readiness',
  'cleanup',
  'canonical_event',
  'real_sink',
  'identity_consistency',
  'exposure_and_metric',
  'migration_execution',
  'rollback_or_forward_compatibility',
  'real_database_integration',
  'unauthorized_path',
  'authorized_path',
  'secret_and_permission_checks',
  'decimal_and_rounding',
  'idempotency',
  'retry_behavior',
  'race_behavior',
  'uniqueness',
  'transactionality',
])

const requirementRiskSchema = z.enum(['low', 'medium', 'high'])
const planItemDispositionSchema = z.enum([
  'required_in_github_ci',
  'unverified_external_ci',
])

const testCommandSchema = z
  .object({
    command: z.string().min(1),
    cwd: z.string().min(1),
    source_path: z.string().min(1),
  })
  .strict()

const verificationPlanItemSchema = z
  .object({
    plan_item_id: z.string().regex(/^VP-[0-9]{3}$/),
    requirement_id: z.string().regex(/^REQ-[0-9]{3}$/),
    surface: verificationSurfaceSchema,
    policy_check: verificationCheckSchema,
    requirement_risk: requirementRiskSchema,
    expected_assertion: z.string().min(1),
    expected_ci_evidence_ids: z.array(z.string()).min(1),
    requires_changed_test_for_pr: z.boolean(),
    disposition: planItemDispositionSchema,
  })
  .strict()
  .refine(
    (item) => new Set(item.expected_ci_evidence_ids).size === item.expected_ci_evidence_ids.length,
    { message: 'plan-item CI evidence IDs must be unique' },
  )

export const verificationPlanSchema = z
  .object({
    schema_version: z.literal('verification_plan@1'),
    source_ledger_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    repo_profile_schema_version: z.string().min(1),
    risk: requirementRiskSchema,
    authority: z.literal('github_ci'),
    apdl_local_execution_authoritative: z.literal(false),
    workflow_gate_policy: z.literal('preserve_or_strengthen'),
    test_runner_configured: z.boolean(),
    test_commands: z.array(testCommandSchema),
    github_workflow_paths: z.array(z.string()),
    protected_workflow_paths: z.array(z.string()),
    disposition: z.enum([
      'github_ci_planned',
      'unverified_external_ci',
      'no_implementable_requirements',
    ]),
    disposition_reason: z.string().min(1),
    items: z.array(verificationPlanItemSchema),
  })
  .strict()
  .superRefine((plan, ctx) => {
    const expectedItemIds = plan.items.map((_, index) => `VP-${String(index + 1).padStart(3, '0')}`)
    if (plan.items.some((item, index) => item.plan_item_id !== expectedItemIds[index])) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'plan item IDs must be contiguous and ordered from VP-001' })
    }

    const sortedWorkflows = [...new Set(plan.github_workflow_paths)].sort()
    if (JSON.stringify(plan.github_workflow_paths) !== JSON.stringify(sortedWorkflows)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'GitHub workflow paths must be sorted and unique' })
    }
    const sortedProtected = [...new Set(plan.protected_workflow_paths)].sort()
    if (JSON.stringify(plan.protected_workflow_paths) !== JSON.stringify(sortedProtected)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'protected workflow paths must be sorted and unique' })
    }
    if (plan.protected_workflow_paths.some((path) => !plan.github_workflow_paths.includes(path))) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'protected workflows must be known GitHub workflows' })
    }

    if (plan.disposition === 'github_ci_planned') {
      if (!plan.test_runner_configured || plan.github_workflow_paths.length === 0) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'github_ci_planned requires a test runner and GitHub workflow' })
      }
      if (plan.items.some((item) => item.disposition !== 'required_in_github_ci')) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'planned GitHub CI items must be required in GitHub CI' })
      }
    } else if (plan.disposition === 'unverified_external_ci') {
      if (plan.items.some((item) => item.disposition !== 'unverified_external_ci')) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'unverified plans must mark every item unverified' })
      }
    } else if (plan.items.length > 0) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'no_implementable_requirements plans cannot contain items' })
    }
  })

const verificationCoverageItemSchema = z
  .object({
    plan_item_id: z.string().regex(/^VP-[0-9]{3}$/),
    status: z.enum([
      'coverage_path_present',
      'missing_required_coverage',
      'planned_in_github_ci',
      'unverified_external_ci',
      'requires_protected_workflow_review',
      'rejected_workflow_gate_relaxation',
    ]),
    coverage_paths: z.array(z.string()),
  })
  .strict()

export const verificationCoverageSchema = z
  .object({
    schema_version: z.literal('verification_coverage@1'),
    source_ledger_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    authority: z.literal('github_ci'),
    github_has_not_reported: z.literal(true),
    apdl_declared_verified: z.literal(false),
    workflow_gate_policy: z.literal('preserve_or_strengthen'),
    disposition: z.enum([
      'ready_for_github_ci',
      'missing_required_coverage',
      'unverified_external_ci',
      'requires_protected_workflow_review',
      'rejected_workflow_gate_relaxation',
      'no_implementable_requirements',
    ]),
    disposition_reason: z.string().min(1),
    changed_test_paths: z.array(z.string()),
    changed_workflow_paths: z.array(z.string()),
    changed_protected_workflow_paths: z.array(z.string()),
    relaxed_workflow_paths: z.array(z.string()),
    items: z.array(verificationCoverageItemSchema),
  })
  .strict()
  .superRefine((coverage, ctx) => {
    const pathFields = [
      'changed_test_paths',
      'changed_workflow_paths',
      'changed_protected_workflow_paths',
      'relaxed_workflow_paths',
    ] as const
    for (const field of pathFields) {
      const sorted = [...new Set(coverage[field])].sort()
      if (JSON.stringify(coverage[field]) !== JSON.stringify(sorted)) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: `${field} must be sorted and unique`, path: [field] })
      }
    }
    if (
      coverage.changed_protected_workflow_paths.some(
        (path) => !coverage.changed_workflow_paths.includes(path),
      )
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'changed protected workflows must be changed workflows',
      })
    }
  })

export const KNOWN_CHANGESET_STATUSES = [
  'queued',
  'cloning',
  'editing',
  'testing',
  'tests_failed',
  'pushing',
  'pr_open',
  'ci_running',
  'ci_failed',
  'ci_passed',
  'unverified_external_ci',
  'waiting_approval',
  'merged',
  'abandoned',
  'error',
] as const

export const TERMINAL_CHANGESET_STATUSES = new Set<string>([
  'tests_failed',
  'merged',
  'abandoned',
  'error',
])

// Failed outcomes the codegen service will re-run via POST /{id}/retry (mirrors
// RETRYABLE_STATUSES in services/codegen/app/models/changeset.py). A retry
// enqueues a fresh changeset carrying the same task; `merged` rolls back with
// /revert instead, and in-flight statuses are still running.
export const RETRYABLE_CHANGESET_STATUSES = new Set<string>([
  'tests_failed',
  'ci_failed',
  'error',
  'abandoned',
])

export const changesetSchema = z
  .object({
    changeset_id: z.string(),
    project_id: z.string(),
    run_id: z.string().nullable(),
    task: taskSpecSchema,
    status: z.string(),
    base_branch: z.string().nullable(),
    branch: z.string().nullable(),
    pr_url: z.string().nullable(),
    pr_number: z.number().int().nullable(),
    pr_node_id: z.string().nullable(),
    ci_status: z
      .enum(['pending', 'passed', 'failed', 'unverified_external_ci'])
      .nullable(),
    // Stamped once at pr_open; anchors the CI sync's grace window. Null for
    // pre-PR changesets and rows predating the column.
    ci_awaiting_since: z.string().nullable(),
    ci_retry_count: z.number().int().nonnegative(),
    ci_remediation_status: z.enum(['idle', 'repairing', 'awaiting_ci', 'exhausted']),
    ci_failure_key: z.string().nullable(),
    ci_failure_summary: z.string().nullable(),
    merge_sha: z.string().nullable(),
    diff_stat: z.record(z.unknown()),
    prompts: z.array(changesetPromptSchema),
    contract_bundle: contractBundleSchema.nullable(),
    requirement_ledger: requirementLedgerSchema.nullable(),
    inspection_snapshot: inspectionSnapshotSchema.nullable(),
    dependency_slice: dependencySliceSchema.nullable(),
    verification_plan: verificationPlanSchema.nullable(),
    verification_coverage: verificationCoverageSchema.nullable(),
    error: z.string().nullable(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const changesetListSchema = z.array(changesetSchema)

// Repo connection registry (services/codegen/app/models/connection.py):
// binds a project to a GitHub App installation + repository.
export const REPO_SLUG_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/

export const repoConnectionSchema = z
  .object({
    project_id: z.string(),
    installation_id: z.number().int(),
    repo: z.string(),
    default_base_branch: z.string(),
    policy: z.record(z.unknown()),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const repoConnectionCreateSchema = z
  .object({
    project_id: z.string().min(1),
    // Omitted → the codegen service resolves the live installation id from the
    // repo slug (and 422s cleanly if the App is not installed on it).
    installation_id: z.number().int().min(1).optional(),
    repo: z.string().regex(REPO_SLUG_PATTERN, 'Format: owner/name'),
    default_base_branch: z.string().min(1),
  })
  .strict()

// One repository the APDL GitHub App can reach (codegen GET /v1/github/repos —
// mirrors AccessibleRepo in services/codegen/app/github/installations.py).
export const accessibleRepoSchema = z
  .object({
    repo: z.string(),
    installation_id: z.number().int(),
    account: z.string(),
    default_branch: z.string(),
    private: z.boolean(),
  })
  .strict()

export const accessibleRepoListSchema = z.array(accessibleRepoSchema)
