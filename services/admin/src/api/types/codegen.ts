import type { z } from 'zod'

import type {
  accessibleRepoSchema,
  changesetPromptSchema,
  changesetSchema,
  contractBundleSchema,
  requirementLedgerSchema,
  repoConnectionCreateSchema,
  repoConnectionSchema,
  taskSpecSchema,
} from '../schemas/codegen'

export type TaskSpec = z.infer<typeof taskSpecSchema>
export type ChangesetPrompt = z.infer<typeof changesetPromptSchema>
export type Changeset = z.infer<typeof changesetSchema>
export type ContractBundle = z.infer<typeof contractBundleSchema>
export type RequirementLedger = z.infer<typeof requirementLedgerSchema>
export type RepoConnection = z.infer<typeof repoConnectionSchema>
export type RepoConnectionCreate = z.infer<typeof repoConnectionCreateSchema>
export type AccessibleRepo = z.infer<typeof accessibleRepoSchema>
