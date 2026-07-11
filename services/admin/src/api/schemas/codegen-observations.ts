import { z } from 'zod'

export const githubPRStatusSchema = z.enum(['draft', 'open', 'merged', 'closed'])
export const externalCIStatusSchema = z.enum([
  'pending',
  'passed',
  'failed',
  'unverified_external_ci',
])
export const ciRemediationStatusSchema = z.enum([
  'idle',
  'diagnosing',
  'repairing',
  'awaiting_ci',
  'resolved',
  'exhausted',
])

const awareDateTimeSchema = z.string().datetime({ offset: true })
const repositorySchema = z.string().regex(/^[^/]+\/[^/]+$/)

const checkAnnotationSchema = z
  .object({
    path: z.string().min(1),
    start_line: z.number().int().min(1).nullable(),
    end_line: z.number().int().min(1).nullable(),
    level: z.enum(['notice', 'warning', 'failure']),
    message: z.string().min(1),
  })
  .strict()
  .refine(
    (annotation) =>
      annotation.start_line === null ||
      annotation.end_line === null ||
      annotation.end_line >= annotation.start_line,
    { message: 'annotation end_line cannot precede start_line' },
  )

const ciSignalSchema = z
  .object({
    signal_id: z.string().min(1),
    kind: z.enum(['check_run', 'commit_status']),
    name: z.string().min(1),
    conclusion: z.enum(['pending', 'passed', 'failed', 'neutral', 'skipped']),
    github_url: z.string().nullable(),
    check_suite_id: z.number().int().min(1).nullable(),
    check_run_id: z.number().int().min(1).nullable(),
    summary: z.string().nullable(),
    annotations: z.array(checkAnnotationSchema),
  })
  .strict()
  .superRefine((signal, ctx) => {
    if (signal.kind === 'check_run') {
      if (signal.check_run_id === null) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'check_run signals require check_run_id' })
      } else if (signal.signal_id !== `check_run:${signal.check_run_id}`) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'check_run signal_id must be derived from check_run_id' })
      }
    } else if (signal.check_run_id !== null || signal.check_suite_id !== null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'commit_status signals cannot carry check identifiers' })
    }
  })

const requirementCIResultSchema = z
  .object({
    requirement_id: z.string().regex(/^REQ-[0-9]{3}$/),
    evidence_id: z.string().regex(/^CI-REQ-[0-9]{3}-[0-9]{2}$/),
    status: z.enum(['pending', 'passed', 'failed', 'unverified']),
    matched_signal_ids: z.array(z.string()),
    explanation: z.string().min(1),
  })
  .strict()
  .superRefine((result, ctx) => {
    if (!result.evidence_id.startsWith(`CI-${result.requirement_id}-`)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'evidence_id must be namespaced by requirement_id' })
    }
    if (
      (result.status === 'passed' || result.status === 'failed') &&
      result.matched_signal_ids.length === 0
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'passed or failed requirement results need a matched signal' })
    }
    if (new Set(result.matched_signal_ids).size !== result.matched_signal_ids.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'matched signal IDs must be unique' })
    }
  })

export const ciVerificationObservationSchema = z
  .object({
    schema_version: z.literal('ci_verification_observation@1'),
    observation_id: z.string().min(1),
    changeset_id: z.string().min(1),
    repository: repositorySchema,
    pr_number: z.number().int().min(1),
    head_sha: z.string().min(1),
    status: externalCIStatusSchema,
    signals: z.array(ciSignalSchema),
    requirement_results: z.array(requirementCIResultSchema),
    observed_at: awareDateTimeSchema,
    failure_key: z.string().nullable(),
    failure_summary: z.string().nullable(),
  })
  .strict()
  .superRefine((observation, ctx) => {
    const signalIds = observation.signals.map((signal) => signal.signal_id)
    if (new Set(signalIds).size !== signalIds.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'CI signal IDs must be unique' })
    }
    const knownSignalIds = new Set(signalIds)
    const signalsById = new Map(observation.signals.map((signal) => [signal.signal_id, signal]))
    if (
      observation.requirement_results.some((result) =>
        result.matched_signal_ids.some((signalId) => !knownSignalIds.has(signalId)),
      )
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'requirement result refers to an unknown CI signal' })
    }
    for (const result of observation.requirement_results) {
      const matched = result.matched_signal_ids
        .map((signalId) => signalsById.get(signalId))
        .filter((signal) => signal !== undefined)
      if (
        result.status === 'passed' &&
        (!matched.some((signal) => signal.conclusion === 'passed') ||
          matched.some(
            (signal) => signal.conclusion !== 'passed' && signal.conclusion !== 'skipped',
          ))
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'passed requirement CI needs a genuinely passed signal',
        })
      }
      if (
        result.status === 'failed' &&
        !matched.some((signal) => signal.conclusion === 'failed')
      ) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'failed requirement CI needs a failed signal',
        })
      }
    }

    if (observation.status === 'passed') {
      if (observation.signals.length === 0) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'CI cannot pass without an observed signal' })
      }
      if (
        observation.signals.some(
          (signal) => signal.conclusion !== 'passed' && signal.conclusion !== 'skipped',
        )
      ) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'passed CI cannot contain pending, failed, or neutral signals' })
      }
      if (!observation.signals.some((signal) => signal.conclusion === 'passed')) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'CI cannot pass with only skipped signals' })
      }
    }
    if (observation.status === 'unverified_external_ci' && observation.signals.length > 0) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'externally unverified CI cannot contain observed signals' })
    }
    if (observation.status === 'failed') {
      if (!observation.signals.some((signal) => signal.conclusion === 'failed')) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'failed CI requires a failed signal' })
      }
      if (!observation.failure_key || !observation.failure_summary) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'failed CI requires a failure key and summary' })
      }
    } else if (observation.failure_key !== null || observation.failure_summary !== null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'failure evidence is only valid for failed CI' })
    }
  })

export const pullRequestObservationSchema = z
  .object({
    schema_version: z.literal('pull_request_observation@1'),
    observation_id: z.string().min(1),
    delivery_id: z.string().min(1).nullable(),
    changeset_id: z.string().min(1),
    repository: repositorySchema,
    pr_number: z.number().int().min(1),
    head_sha: z.string().min(1),
    status: githubPRStatusSchema,
    action: z.enum([
      'opened',
      'ready_for_review',
      'converted_to_draft',
      'synchronize',
      'closed',
      'reopened',
      'polled',
    ]),
    github_url: z.string().min(1),
    merge_sha: z.string().nullable(),
    github_updated_at: awareDateTimeSchema,
    observed_at: awareDateTimeSchema,
  })
  .strict()
  .superRefine((observation, ctx) => {
    if (observation.status === 'merged' && !observation.merge_sha) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'merged pull-request observations require merge_sha' })
    }
    if (observation.status !== 'merged' && observation.merge_sha !== null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'merge_sha is only valid for a merged pull request' })
    }
    const allowedByAction: Record<typeof observation.action, Set<typeof observation.status>> = {
      opened: new Set(['draft', 'open']),
      ready_for_review: new Set(['open']),
      converted_to_draft: new Set(['draft']),
      synchronize: new Set(['draft', 'open']),
      closed: new Set(['closed', 'merged']),
      reopened: new Set(['draft', 'open']),
      polled: new Set(['draft', 'open', 'merged', 'closed']),
    }
    if (!allowedByAction[observation.action].has(observation.status)) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'pull-request action is inconsistent with status' })
    }
  })

const remediationPromptEvidenceSchema = z
  .object({
    evidence_id: z.string().regex(/^prompt:[0-9a-f]{24}$/),
    content_sha256: z.string().regex(/^[0-9a-f]{64}$/),
    stage: z.string().min(1),
    label: z.string().min(1),
    excerpt: z.string().min(1).max(2000),
  })
  .strict()

export const ciRemediationAttemptSchema = z
  .object({
    schema_version: z.literal('ci_remediation_attempt@1'),
    attempt_id: z.string().min(1),
    event_sequence: z.number().int().min(1),
    event_id: z.string().min(1),
    changeset_id: z.string().min(1),
    repository: repositorySchema,
    pr_number: z.number().int().min(1),
    failed_head_sha: z.string().min(1),
    failure_observation_id: z.string().min(1),
    attempt_number: z.number().int().min(1),
    classification: z.enum(['actionable_code', 'flaky', 'infrastructure', 'policy', 'unknown']),
    confidence: z.number().min(0).max(1),
    prompt_evidence_ids: z.array(z.string()),
    prompt_evidence: z.array(remediationPromptEvidenceSchema),
    changed_files: z.array(z.string()),
    resulting_commit_sha: z.string().nullable(),
    disposition: z.enum([
      'diagnosing',
      'awaiting_ci',
      'repaired',
      'rerun_requested',
      'exhausted',
      'superseded',
      'not_actionable',
    ]),
    started_at: awareDateTimeSchema,
    recorded_at: awareDateTimeSchema,
    finished_at: awareDateTimeSchema.nullable(),
    error: z.string().nullable(),
  })
  .strict()
  .superRefine((attempt, ctx) => {
    if (attempt.event_id !== `${attempt.attempt_id}:${attempt.event_sequence}`) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'event_id must be derived from attempt_id and event_sequence' })
    }
    const startedAt = Date.parse(attempt.started_at)
    const recordedAt = Date.parse(attempt.recorded_at)
    const finishedAt = attempt.finished_at === null ? null : Date.parse(attempt.finished_at)
    if (recordedAt < startedAt) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'recorded_at cannot precede started_at' })
    }
    const final = attempt.disposition !== 'diagnosing' && attempt.disposition !== 'awaiting_ci'
    if (final && finishedAt === null) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'final remediation dispositions require finished_at' })
    }
    if (finishedAt !== null && finishedAt < startedAt) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'finished_at cannot precede started_at' })
    }
    if (finishedAt !== null && finishedAt > recordedAt) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'recorded_at cannot precede finished_at' })
    }
    if (
      (attempt.disposition === 'repaired' || attempt.disposition === 'awaiting_ci') &&
      !attempt.resulting_commit_sha
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'a pushed repair requires resulting_commit_sha' })
    }
    if (
      attempt.disposition === 'rerun_requested' &&
      (attempt.changed_files.length > 0 || attempt.resulting_commit_sha !== null)
    ) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'a CI rerun must not claim code changes or a commit' })
    }
    if (new Set(attempt.prompt_evidence_ids).size !== attempt.prompt_evidence_ids.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'prompt evidence IDs must be unique' })
    }
    const embeddedEvidenceIds = attempt.prompt_evidence.map((evidence) => evidence.evidence_id)
    if (JSON.stringify(embeddedEvidenceIds) !== JSON.stringify(attempt.prompt_evidence_ids)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'prompt_evidence_ids must exactly match embedded prompt evidence',
      })
    }
    if (new Set(attempt.changed_files).size !== attempt.changed_files.length) {
      ctx.addIssue({ code: z.ZodIssueCode.custom, message: 'changed files must be unique' })
    }
  })

export const changesetObservationHistorySchema = z
  .object({
    schema_version: z.literal('changeset_observation_history@1'),
    pull_requests: z.array(pullRequestObservationSchema),
    ci_verifications: z.array(ciVerificationObservationSchema),
    remediation_attempts: z.array(ciRemediationAttemptSchema),
  })
  .strict()
