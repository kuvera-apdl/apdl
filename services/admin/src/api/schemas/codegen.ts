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
