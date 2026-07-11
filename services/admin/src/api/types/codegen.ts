import type { z } from 'zod'

import type {
  accessibleRepoSchema,
  changesetPromptSchema,
  changesetSchema,
  contractBundleSchema,
  dependencySliceSchema,
  inspectionSnapshotSchema,
  requirementLedgerSchema,
  repoConnectionCreateSchema,
  repoConnectionSchema,
  taskSpecSchema,
  verificationCoverageSchema,
  verificationPlanSchema,
} from '../schemas/codegen'

export type TaskSpec = z.infer<typeof taskSpecSchema>
export type ChangesetPrompt = z.infer<typeof changesetPromptSchema>
export type Changeset = z.infer<typeof changesetSchema>
export type ContractBundle = z.infer<typeof contractBundleSchema>
export type RequirementLedger = z.infer<typeof requirementLedgerSchema>
export type InspectionSnapshot = z.infer<typeof inspectionSnapshotSchema>
export type DependencySlice = z.infer<typeof dependencySliceSchema>
export type VerificationPlan = z.infer<typeof verificationPlanSchema>
export type VerificationCoverage = z.infer<typeof verificationCoverageSchema>
export type RepoConnection = z.infer<typeof repoConnectionSchema>
export type RepoConnectionCreate = z.infer<typeof repoConnectionCreateSchema>
export type AccessibleRepo = z.infer<typeof accessibleRepoSchema>
