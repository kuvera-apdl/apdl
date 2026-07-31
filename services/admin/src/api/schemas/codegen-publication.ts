import { z } from 'zod'

import {
  codegenLlmProjectIdSchema,
  codegenLlmProviderSchema,
  codegenLlmRoleSchema,
} from './codegen-llm-connections'

const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/)
const dockerImageIdSchema = z.string().regex(/^sha256:[0-9a-f]{64}$/)
const codegenRevisionSchema = z
  .string()
  .min(1)
  .max(200)
  .refine((value) => value === value.trim(), 'codegen revision must be normalized')

export const codegenRiskLevelSchema = z.enum(['low', 'medium', 'high'])

export const codegenLlmAssignmentSnapshotSchema = z
  .object({
    schema_version: z.literal('codegen_llm_assignment_snapshot@1'),
    role: codegenLlmRoleSchema,
    provider: codegenLlmProviderSchema,
    model_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    assignment_version: z.number().int().min(1),
    connection_version: z.number().int().min(1),
    inventory_version: z.number().int().min(1),
    catalog_version: z.string().regex(/^codegen-provider-catalog@[1-9][0-9]*$/),
    context_window_tokens: z.number().int().min(16_000),
    supports_tool_calling: z.boolean(),
    supports_structured_output: z.boolean(),
    input_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    output_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
  })
  .strict()

export const codegenLlmExecutionSnapshotSchema = z
  .object({
    schema_version: z.literal('codegen_llm_execution_snapshot@2'),
    project_id: codegenLlmProjectIdSchema,
    repository_grant_id: z
      .string()
      .min(5)
      .max(132)
      .regex(/^ghg_[A-Za-z0-9_-]+$/),
    repository_id: z.number().int().min(1),
    repository_installation_id: z.number().int().min(1),
    repository_full_name: z
      .string()
      .max(201)
      .regex(/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/),
    codegen_revision: codegenRevisionSchema,
    behavior_configuration_sha256: sha256Schema,
    rollout_stage: z.enum([
      'offline',
      'development_pr',
      'tenant_draft_pr',
    ]),
    assignments: z.tuple([
      codegenLlmAssignmentSnapshotSchema,
      codegenLlmAssignmentSnapshotSchema,
    ]),
  })
  .strict()
  .superRefine((snapshot, context) => {
    if (snapshot.assignments[0].role !== 'editor') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['assignments', 0, 'role'],
        message: 'the first assignment must be the editor assignment',
      })
    }
    if (snapshot.assignments[1].role !== 'helper') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['assignments', 1, 'role'],
        message: 'the second assignment must be the helper assignment',
      })
    }
  })

export const tenantPublicationRuntimeIdentitySchema = z
  .object({
    schema_version: z.literal('tenant_publication_runtime_identity@1'),
    controller_image_id: dockerImageIdSchema,
    worker_image_id: dockerImageIdSchema,
    codegen_revision: codegenRevisionSchema.refine((value) => value === value.trim(), {
      message: 'runtime codegen_revision must be normalized',
    }),
    behavior_configuration_sha256: sha256Schema,
    egress_policy_sha256: sha256Schema,
    egress_proxy_image_id: dockerImageIdSchema,
    egress_transport: z.literal('network_none_unix_socket@1'),
    max_concurrent_jobs: z.literal(1),
    identity_sha256: sha256Schema,
  })
  .strict()

export const tenantPublicationRequestSchema = z
  .object({
    schema_version: z.literal('tenant_publication_request@1'),
    requested_stage: z.literal('tenant_draft_pr'),
    risk: codegenRiskLevelSchema,
    execution_snapshot: codegenLlmExecutionSnapshotSchema,
    execution_snapshot_sha256: sha256Schema,
    runtime_identity: tenantPublicationRuntimeIdentitySchema,
  })
  .strict()
  .superRefine((request, context) => {
    if (request.execution_snapshot.rollout_stage !== request.requested_stage) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['execution_snapshot', 'rollout_stage'],
        message: 'the execution snapshot must target the requested tenant stage',
      })
    }
    if (
      request.execution_snapshot.codegen_revision !==
      request.runtime_identity.codegen_revision
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['runtime_identity', 'codegen_revision'],
        message: 'the runtime and execution snapshot revisions must match',
      })
    }
    if (
      request.execution_snapshot.behavior_configuration_sha256 !==
      request.runtime_identity.behavior_configuration_sha256
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['runtime_identity', 'behavior_configuration_sha256'],
        message: 'the runtime and execution behavior identities must match',
      })
    }
  })

export const tenantPublicationDecisionSchema = z
  .object({
    schema_version: z.literal('tenant_publication_decision@1'),
    requested_stage: z.literal('tenant_draft_pr'),
    risk: codegenRiskLevelSchema,
    allowed: z.literal(true),
    publish_branch: z.literal(true),
    create_pull_request: z.literal(true),
    ready_for_review: z.literal(false),
    reasons: z.tuple([]),
    decision_sha256: sha256Schema,
  })
  .strict()

export const tenantPublicationAuthorizationSchema = z
  .object({
    schema_version: z.literal('tenant_publication_authorization@1'),
    authority: z.literal('tenant_model_assignments'),
    request: tenantPublicationRequestSchema,
    decision: tenantPublicationDecisionSchema,
    draft_only: z.literal(true),
    authorization_sha256: sha256Schema,
  })
  .strict()
  .superRefine((authorization, context) => {
    if (
      authorization.decision.requested_stage !==
      authorization.request.requested_stage
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['decision', 'requested_stage'],
        message: 'the publication decision stage must match its request',
      })
    }
    if (authorization.decision.risk !== authorization.request.risk) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['decision', 'risk'],
        message: 'the publication decision risk must match its request',
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
  .superRefine((authorization, context) => {
    if (authorization.request.risk !== authorization.decision.risk) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['decision', 'risk'],
        message: 'development publication decision risk must match its request',
      })
    }
  })

export const publicationAuthorizationSchema = z.union([
  tenantPublicationAuthorizationSchema,
  developmentPublicationAuthorizationSchema,
])
