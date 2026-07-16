import type { z } from 'zod'

import type {
  changesetPromptSchema,
  changesetSchema,
  contractBundleSchema,
  dependencySliceSchema,
  inspectionSnapshotSchema,
  requirementLedgerSchema,
  reviewVerdictSchema,
  repoConnectionSchema,
  taskSpecSchema,
  verificationCoverageSchema,
  verificationPlanSchema,
} from '../schemas/codegen'
import type {
  changesetObservationHistorySchema,
  ciRemediationAttemptSchema,
  ciVerificationObservationSchema,
  pullRequestObservationSchema,
} from '../schemas/codegen-observations'
import type {
  runtimeAcceptancePlanSchema,
  runtimeEvidenceAssessmentSchema,
  runtimeEvidenceObservationSchema,
} from '../schemas/codegen-runtime'
import type {
  publicationAuthorizationSchema,
  publicationRequestSchema,
  rolloutDecisionSchema,
} from '../schemas/codegen-publication'

export type TaskSpec = z.infer<typeof taskSpecSchema>
export type ChangesetPrompt = z.infer<typeof changesetPromptSchema>
export type Changeset = z.infer<typeof changesetSchema>
export type ContractBundle = z.infer<typeof contractBundleSchema>
export type RequirementLedger = z.infer<typeof requirementLedgerSchema>
export type InspectionSnapshot = z.infer<typeof inspectionSnapshotSchema>
export type DependencySlice = z.infer<typeof dependencySliceSchema>
export type VerificationPlan = z.infer<typeof verificationPlanSchema>
export type VerificationCoverage = z.infer<typeof verificationCoverageSchema>
export type RuntimeAcceptancePlan = z.infer<typeof runtimeAcceptancePlanSchema>
export type RuntimeEvidenceAssessment = z.infer<typeof runtimeEvidenceAssessmentSchema>
export type RuntimeEvidenceObservation = z.infer<typeof runtimeEvidenceObservationSchema>
export type ReviewVerdict = z.infer<typeof reviewVerdictSchema>
export type PublicationRequest = z.infer<typeof publicationRequestSchema>
export type RolloutDecision = z.infer<typeof rolloutDecisionSchema>
export type PublicationAuthorization = z.infer<typeof publicationAuthorizationSchema>
export type PullRequestObservation = z.infer<typeof pullRequestObservationSchema>
export type CIVerificationObservation = z.infer<typeof ciVerificationObservationSchema>
export type CIRemediationAttempt = z.infer<typeof ciRemediationAttemptSchema>
export type ChangesetObservationHistory = z.infer<typeof changesetObservationHistorySchema>
export type RepoConnection = z.infer<typeof repoConnectionSchema>
