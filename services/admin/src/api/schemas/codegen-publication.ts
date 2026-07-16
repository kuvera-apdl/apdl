import { z } from 'zod'

const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/)

export const codegenRiskLevelSchema = z.enum(['low', 'medium', 'high'])
export const rolloutStageSchema = z.enum([
  'offline',
  'shadow',
  'development_pr',
  'reviewed_pr',
  'low_risk_canary',
])

export const publicationRequestSchema = z
  .object({
    schema_version: z.literal('publication_request@2'),
    requested_stage: rolloutStageSchema,
    risk: codegenRiskLevelSchema,
    model: z.string().min(1),
    codegen_revision: z.string().min(1),
    candidate_identity_sha256: sha256Schema,
    canary_identity: z.string().min(1).max(500).nullable(),
  })
  .strict()
  .superRefine((request, ctx) => {
    if (!['reviewed_pr', 'low_risk_canary'].includes(request.requested_stage)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication requests must target a PR publication stage',
      })
    }
    if (request.requested_stage === 'low_risk_canary' && request.canary_identity === null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'canary publication requires a stable identity',
      })
    }
    if (request.requested_stage !== 'low_risk_canary' && request.canary_identity !== null) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'canary_identity is valid only for the canary stage',
      })
    }
  })

export const rolloutDecisionSchema = z
  .object({
    schema_version: z.literal('rollout_decision@2'),
    requested_stage: rolloutStageSchema,
    risk: codegenRiskLevelSchema,
    allowed: z.boolean(),
    publish_branch: z.boolean(),
    create_pull_request: z.boolean(),
    ready_for_review: z.boolean(),
    reasons: z.array(z.string()),
    evaluation_summary_sha256: sha256Schema.nullable(),
    policy_sha256: sha256Schema,
    canary_identity_sha256: sha256Schema.nullable(),
    canary_bucket: z.number().int().min(0).max(99).nullable(),
    decision_sha256: sha256Schema,
  })
  .strict()
  .superRefine((decision, ctx) => {
    const publishing =
      decision.publish_branch || decision.create_pull_request || decision.ready_for_review

    if (decision.requested_stage === 'development_pr') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'evaluated rollout decisions cannot target the development stage',
      })
    }

    if (!decision.allowed && publishing) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'a denied rollout cannot grant publication capabilities',
      })
    }
    if (decision.allowed && decision.reasons.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'an allowed rollout cannot contain denial reasons',
      })
    }
    if (!decision.allowed && decision.reasons.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'a denied rollout requires at least one reason',
      })
    }

    if (decision.requested_stage === 'offline' || decision.requested_stage === 'shadow') {
      if (publishing) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'offline and shadow stages cannot publish',
        })
      }
      if (!decision.allowed) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'offline and shadow execution is always allowed',
        })
      }
    } else if (decision.allowed) {
      if (!decision.publish_branch || !decision.create_pull_request) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'an allowed PR stage must grant branch and PR publication',
        })
      }
      const expectedReady = decision.requested_stage === 'low_risk_canary'
      if (decision.ready_for_review !== expectedReady) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'ready-for-review is granted only to an allowed canary',
        })
      }
      if (decision.evaluation_summary_sha256 === null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'publication requires an evaluation summary digest',
        })
      }
    }
  })

const evaluatedPublicationAuthorizationSchema = z
  .object({
    schema_version: z.literal('publication_authorization@2'),
    request: publicationRequestSchema,
    expected_model: z.string().min(1),
    expected_codegen_revision: z.string().min(1),
    expected_candidate_identity_sha256: sha256Schema,
    report_sha256: sha256Schema,
    bundle_sha256: sha256Schema,
    policy_sha256: sha256Schema,
    decision: rolloutDecisionSchema,
    authorization_sha256: sha256Schema,
  })
  .strict()
  .superRefine((authorization, ctx) => {
    if (authorization.request.model !== authorization.expected_model) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication request model does not match expected_model',
      })
    }
    if (authorization.request.codegen_revision !== authorization.expected_codegen_revision) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication request revision does not match expected_codegen_revision',
      })
    }
    if (
      authorization.request.candidate_identity_sha256 !==
      authorization.expected_candidate_identity_sha256
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication request candidate identity does not match expected identity',
      })
    }
    if (authorization.decision.requested_stage !== authorization.request.requested_stage) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication decision stage does not match its request',
      })
    }
    if (authorization.decision.risk !== authorization.request.risk) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication decision risk does not match its request',
      })
    }
    if (authorization.decision.policy_sha256 !== authorization.policy_sha256) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'publication decision does not use the bundled policy',
      })
    }
  })

export const developmentPublicationRequestSchema = z
  .object({
    schema_version: z.literal('development_publication_request@1'),
    requested_stage: z.literal('development_pr'),
    risk: codegenRiskLevelSchema,
    model: z.string().min(1),
    codegen_revision: z.literal('local-development'),
  })
  .strict()

export const developmentPublicationDecisionSchema = z
  .object({
    schema_version: z.literal('development_publication_decision@1'),
    requested_stage: z.literal('development_pr'),
    risk: codegenRiskLevelSchema,
    allowed: z.literal(true),
    publish_branch: z.literal(true),
    create_pull_request: z.literal(true),
    ready_for_review: z.literal(false),
    reasons: z.tuple([]),
    decision_sha256: sha256Schema,
  })
  .strict()

export const developmentPublicationAuthorizationSchema = z
  .object({
    schema_version: z.literal('development_publication_authorization@1'),
    authority: z.literal('local_development'),
    request: developmentPublicationRequestSchema,
    decision: developmentPublicationDecisionSchema,
    draft_only: z.literal(true),
    authorization_sha256: sha256Schema,
  })
  .strict()
  .superRefine((authorization, ctx) => {
    if (authorization.request.risk !== authorization.decision.risk) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'development publication decision risk does not match its request',
      })
    }
  })

export const publicationAuthorizationSchema = z.union([
  evaluatedPublicationAuthorizationSchema,
  developmentPublicationAuthorizationSchema,
])
