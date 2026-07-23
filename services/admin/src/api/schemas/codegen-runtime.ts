import { z } from 'zod'

import { externalCIStatusSchema } from './codegen-observations'
import { externalHttpsUrlSchema } from './urls'

const requirementIdSchema = z.string().regex(/^REQ-[0-9]{3}$/)
const headShaSchema = z.string().regex(/^[A-Za-z0-9._-]{1,128}$/)
const artifactNameSchema = z.string().regex(/^[A-Za-z0-9_.-]{1,128}$/)
const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/)
const repositorySchema = z.string().regex(/^[^/\s]+\/[^/\s]+$/)

function isSortedUnique(values: string[]): boolean {
  return values.length === new Set(values).size && values.every(
    (value, index) => index === 0 || values[index - 1].localeCompare(value) < 0,
  )
}

function isCanonicalRelativePath(value: string): boolean {
  return (
    value.length > 0 &&
    !value.startsWith('/') &&
    !value.startsWith('./') &&
    !value.includes('\\') &&
    !value.includes('\0') &&
    !value.includes('\r') &&
    !value.includes('\n') &&
    !value.split('/').includes('..')
  )
}

const canonicalRelativePathSchema = z.string().refine(isCanonicalRelativePath, {
  message: 'path must be canonical and repository-relative',
})

export const runtimeSurfaceSchema = z.enum([
  'browser',
  'api',
  'service_container',
  'runtime',
])

export const runtimeEvidenceKindSchema = z.enum([
  'screenshot',
  'request_trace',
  'emitted_events',
  'measurements',
  'browser_report',
  'server_log',
  'structured_runtime',
])

export const runtimeEvidenceStatusSchema = z.enum(['observed', 'unverified'])

const runtimeRequirementSchema = z
  .object({
    requirement_id: requirementIdSchema,
    surface: runtimeSurfaceSchema,
  })
  .strict()

const runtimeCommandSchema = z
  .object({
    command: z.string().min(1).max(1000).refine(
      (value) => !value.includes('\n') && !value.includes('\r') && !value.includes('\0'),
      { message: 'runtime command must be a single line' },
    ),
    cwd: z.union([z.literal('.'), canonicalRelativePathSchema]),
    source_path: z.union([z.literal('.'), canonicalRelativePathSchema]),
  })
  .strict()

const runtimeArtifactExpectationSchema = z
  .object({
    schema_version: z.literal('runtime_artifact_expectation@1'),
    artifact_name: artifactNameSchema,
    evidence_kind: runtimeEvidenceKindSchema,
    paths: z.array(canonicalRelativePathSchema).min(1),
    requirement_ids: z.array(requirementIdSchema).min(1),
    required: z.boolean(),
  })
  .strict()
  .superRefine((expectation, ctx) => {
    if (!isSortedUnique(expectation.paths)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifact paths must be sorted and unique' })
    }
    if (!isSortedUnique(expectation.requirement_ids)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'requirement IDs must be sorted and unique' })
    }
  })

const runtimeCheckSchema = z
  .object({
    check_id: z.string().regex(/^runtime_[0-9a-f]{16}$/),
    surface: runtimeSurfaceSchema,
    requirement_ids: z.array(requirementIdSchema).min(1),
    command: runtimeCommandSchema,
    service_container_paths: z.array(canonicalRelativePathSchema),
    expected_artifacts: z.array(runtimeArtifactExpectationSchema).min(1),
  })
  .strict()
  .superRefine((check, ctx) => {
    if (!isSortedUnique(check.requirement_ids)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'requirement IDs must be sorted and unique' })
    }
    if (!isSortedUnique(check.service_container_paths)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'container paths must be sorted and unique' })
    }

    const artifactNames = check.expected_artifacts.map((artifact) => artifact.artifact_name)
    if (!isSortedUnique(artifactNames)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifact expectations must be sorted and unique' })
    }

    const knownRequirements = new Set(check.requirement_ids)
    const covered = new Set<string>()
    for (const artifact of check.expected_artifacts) {
      for (const requirementId of artifact.requirement_ids) {
        if (!knownRequirements.has(requirementId)) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifact references an unknown check requirement' })
        }
        if (artifact.required) covered.add(requirementId)
      }
    }
    if (
      covered.size !== knownRequirements.size ||
      [...knownRequirements].some((requirementId) => !covered.has(requirementId))
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'required artifacts must cover every check requirement' })
    }

    if (check.surface === 'service_container' && check.service_container_paths.length === 0) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'service-container checks require deployment paths' })
    }
    if (check.surface !== 'service_container' && check.service_container_paths.length > 0) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'only service-container checks may carry deployment paths' })
    }
  })

const runtimeBlockerSchema = z
  .object({
    requirement_id: requirementIdSchema,
    surface: runtimeSurfaceSchema,
    reason: z.string().min(1),
    evidence_paths: z.array(canonicalRelativePathSchema),
  })
  .strict()
  .refine((blocker) => isSortedUnique(blocker.evidence_paths), {
    message: 'runtime blocker evidence paths must be sorted and unique',
  })

const generatedRuntimeWorkflowExpectationSchema = z
  .object({
    schema_version: z.literal('generated_runtime_workflow_expectation@1'),
    renderer: z.literal('apdl_github_actions_runtime@1'),
    path: canonicalRelativePathSchema.refine(
      (path) => path.startsWith('.github/workflows/') && /\.ya?ml$/.test(path),
      'generated runtime workflow must be a GitHub workflow',
    ),
    content_sha256: sha256Schema,
  })
  .strict()

export const runtimeAcceptancePlanSchema = z
  .object({
    schema_version: z.literal('runtime_acceptance_plan@1'),
    source_ledger_sha256: sha256Schema,
    repo_profile_sha256: sha256Schema,
    verification_plan_sha256: sha256Schema,
    repo: z.string().nullable(),
    branch: z.string().nullable(),
    checks: z.array(runtimeCheckSchema),
    blockers: z.array(runtimeBlockerSchema),
    generated_workflow: generatedRuntimeWorkflowExpectationSchema.nullable(),
  })
  .strict()
  .superRefine((plan, ctx) => {
    if (plan.generated_workflow !== null && plan.checks.length === 0) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'a generated workflow requires executable checks' })
    }
    const checkIds = plan.checks.map((check) => check.check_id)
    if (new Set(checkIds).size !== checkIds.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime check IDs must be unique' })
    }

    const artifactNames = plan.checks.flatMap((check) =>
      check.expected_artifacts.map((artifact) => artifact.artifact_name),
    )
    if (new Set(artifactNames).size !== artifactNames.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime artifact names must be globally unique' })
    }

    const sortedChecks = [...plan.checks].sort((left, right) =>
      left.surface.localeCompare(right.surface) || left.check_id.localeCompare(right.check_id),
    )
    if (plan.checks.some((check, index) => check !== sortedChecks[index])) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime checks must be deterministically sorted' })
    }

    const sortedBlockers = [...plan.blockers].sort((left, right) =>
      left.requirement_id.localeCompare(right.requirement_id) || left.surface.localeCompare(right.surface),
    )
    if (plan.blockers.some((blocker, index) => blocker !== sortedBlockers[index])) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime blockers must be deterministically sorted' })
    }

    const checkedPairs = plan.checks.flatMap((check) =>
      check.requirement_ids.map((requirementId) => `${requirementId}:${check.surface}`),
    )
    const blockedPairs = plan.blockers.map(
      (blocker) => `${blocker.requirement_id}:${blocker.surface}`,
    )
    if (new Set(checkedPairs).size !== checkedPairs.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime requirement surfaces may have only one check' })
    }
    if (new Set(blockedPairs).size !== blockedPairs.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime requirement surfaces may have only one blocker' })
    }
    const checked = new Set(checkedPairs)
    if (blockedPairs.some((pair) => checked.has(pair))) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime requirement surfaces cannot be checked and blocked' })
    }
  })

const artifactFileEvidenceSchema = z
  .object({
    schema_version: z.literal('runtime_artifact_file@1'),
    path: canonicalRelativePathSchema,
    content_sha256: sha256Schema,
    byte_count: z.number().int().nonnegative(),
    text_excerpt: z.string().max(8000).nullable(),
    redacted: z.boolean(),
    binary: z.boolean(),
  })
  .strict()
  .superRefine((file, ctx) => {
    if (file.binary && file.text_excerpt !== null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'binary evidence cannot carry a text excerpt' })
    }
    if (file.binary && file.redacted) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'binary evidence cannot be text-redacted' })
    }
    if (file.redacted && file.text_excerpt === null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'redacted evidence requires a text excerpt' })
    }
  })

const runtimeJobLogEvidenceSchema = z
  .object({
    schema_version: z.literal('runtime_job_log_evidence@1'),
    workflow_run_id: z.number().int().min(1),
    job_id: z.number().int().min(1),
    job_name: z.string().min(1).max(300),
    head_sha: headShaSchema,
    text_excerpt: z.string().max(8000),
    excerpt_byte_count: z.number().int().min(0).max(8000),
    source_byte_count: z.number().int().nonnegative(),
    truncated: z.boolean(),
    redacted: z.boolean(),
    github_url: externalHttpsUrlSchema,
  })
  .strict()
  .refine(
    (job) => new TextEncoder().encode(job.text_excerpt).byteLength === job.excerpt_byte_count,
    { message: 'excerpt_byte_count must match the retained UTF-8 excerpt' },
  )

const runtimeArtifactObservationSchema = z
  .object({
    schema_version: z.literal('runtime_artifact_observation@1'),
    artifact_name: artifactNameSchema,
    artifact_id: z.number().int().min(1).nullable(),
    workflow_run_id: z.number().int().min(1),
    head_sha: headShaSchema,
    status: runtimeEvidenceStatusSchema,
    requirement_ids: z.array(requirementIdSchema).min(1),
    files: z.array(artifactFileEvidenceSchema),
    github_url: externalHttpsUrlSchema.nullable(),
    unverified_reason: z.string().nullable(),
  })
  .strict()
  .superRefine((artifact, ctx) => {
    if (!isSortedUnique(artifact.requirement_ids)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifact requirement IDs must be sorted and unique' })
    }
    const paths = artifact.files.map((file) => file.path)
    if (!isSortedUnique(paths)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifact files must be path-sorted and unique' })
    }
    if (artifact.status === 'observed') {
      if (artifact.artifact_id === null || artifact.files.length === 0) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'observed artifacts require an ID and file evidence' })
      }
      if (artifact.unverified_reason !== null) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'observed artifacts cannot carry an unverified reason' })
      }
    } else {
      if (!artifact.unverified_reason?.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'unverified artifacts require a reason' })
      }
      if (artifact.files.length > 0) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'unverified artifacts cannot carry file evidence' })
      }
    }
  })

const requirementRuntimeEvidenceSchema = z
  .object({
    requirement_id: requirementIdSchema,
    status: runtimeEvidenceStatusSchema,
    artifact_names: z.array(artifactNameSchema),
    reason: z.string().nullable(),
  })
  .strict()
  .superRefine((result, ctx) => {
    if (!isSortedUnique(result.artifact_names)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime artifact names must be sorted and unique' })
    }
    if (result.status === 'observed') {
      if (result.artifact_names.length === 0) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'observed runtime evidence requires an artifact' })
      }
      if (result.reason !== null) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'observed runtime evidence cannot carry a reason' })
      }
    } else if (!result.reason?.trim()) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'unverified runtime evidence requires a reason' })
    }
  })

export const runtimeEvidenceAssessmentSchema = z
  .object({
    schema_version: z.literal('runtime_evidence_assessment@1'),
    head_sha: headShaSchema,
    external_ci_status: externalCIStatusSchema,
    requirements: z.array(requirementRuntimeEvidenceSchema),
  })
  .strict()
  .refine(
    (assessment) => isSortedUnique(
      assessment.requirements.map((requirement) => requirement.requirement_id),
    ),
    { message: 'runtime evidence requirements must be sorted and unique' },
  )

export const runtimeEvidenceObservationSchema = z
  .object({
    schema_version: z.literal('runtime_evidence_observation@1'),
    observation_id: z.string().regex(/^runtime_obs_[0-9a-f]{32}$/),
    changeset_id: z.string().min(1).max(200),
    repository: repositorySchema,
    pr_number: z.number().int().min(1),
    head_sha: headShaSchema,
    ci_observation_id: z.string().min(1).max(200),
    ci_evidence_hash: sha256Schema,
    runtime_acceptance_plan_sha256: sha256Schema,
    observed_at: z.string().datetime({ offset: true }),
    artifacts: z.array(runtimeArtifactObservationSchema),
    job_logs: z.array(runtimeJobLogEvidenceSchema),
    assessment: runtimeEvidenceAssessmentSchema,
    collection_errors: z.array(z.string()),
  })
  .strict()
  .superRefine((observation, ctx) => {
    if (!isSortedUnique(observation.collection_errors)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'collection errors must be sorted and unique' })
    }
    if (observation.collection_errors.some(
      (error) => !error.trim() || error.length > 2000 || error.includes('\0') || error.includes('\r'),
    )) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'collection errors must be bounded text' })
    }

    if (observation.assessment.head_sha !== observation.head_sha) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'assessment must use the observation head' })
    }
    if (observation.artifacts.some((artifact) => artifact.head_sha !== observation.head_sha)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifacts must use the observation head' })
    }
    if (observation.job_logs.some((job) => job.head_sha !== observation.head_sha)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'job logs must use the observation head' })
    }

    const artifactKeys = observation.artifacts.map(
      (artifact) => `${artifact.workflow_run_id}:${artifact.artifact_id ?? 0}:${artifact.artifact_name}`,
    )
    if (new Set(artifactKeys).size !== artifactKeys.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime artifact identities must be unique' })
    }
    const artifactIds = observation.artifacts.flatMap((artifact) =>
      artifact.artifact_id === null ? [] : [artifact.artifact_id],
    )
    if (new Set(artifactIds).size !== artifactIds.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'GitHub artifact IDs must be unique' })
    }
    const sortedArtifacts = [...observation.artifacts].sort((left, right) =>
      left.workflow_run_id - right.workflow_run_id ||
      (left.artifact_id ?? 0) - (right.artifact_id ?? 0) ||
      left.artifact_name.localeCompare(right.artifact_name),
    )
    if (observation.artifacts.some((artifact, index) => artifact !== sortedArtifacts[index])) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime artifacts must be deterministically sorted' })
    }

    const jobKeys = observation.job_logs.map((job) => `${job.workflow_run_id}:${job.job_id}`)
    if (new Set(jobKeys).size !== jobKeys.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime job identities must be unique' })
    }
    const jobIds = observation.job_logs.map((job) => job.job_id)
    if (new Set(jobIds).size !== jobIds.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'GitHub job IDs must be unique' })
    }
    const sortedJobs = [...observation.job_logs].sort(
      (left, right) => left.workflow_run_id - right.workflow_run_id || left.job_id - right.job_id,
    )
    if (observation.job_logs.some((job, index) => job !== sortedJobs[index])) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'runtime job logs must be deterministically sorted' })
    }

    const assessedRequirements = new Set(
      observation.assessment.requirements.map((requirement) => requirement.requirement_id),
    )
    if (observation.artifacts.some((artifact) =>
      artifact.requirement_ids.some((requirementId) => !assessedRequirements.has(requirementId)),
    )) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'artifacts cannot reference unassessed requirements' })
    }
    const observedPairs = new Set(
      observation.artifacts
        .filter((artifact) => artifact.status === 'observed')
        .flatMap((artifact) => artifact.requirement_ids.map(
          (requirementId) => `${artifact.artifact_name}:${requirementId}`,
        )),
    )
    if (observation.assessment.requirements.some((requirement) =>
      requirement.artifact_names.some(
        (artifactName) => !observedPairs.has(`${artifactName}:${requirement.requirement_id}`),
      ),
    )) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'assessment must reference observed exact-head artifacts' })
    }
  })

export const runtimeEvidenceObservationListSchema = z.array(runtimeEvidenceObservationSchema)

// Kept exported for canonical schema consumers and tests; plans embed this
// shape, while observations use the concrete evidence contracts above.
export { runtimeRequirementSchema }
