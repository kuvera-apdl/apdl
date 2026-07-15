import type { z } from 'zod'

import type {
  accessibleRepoSchema,
  changesetPromptSchema,
  changesetSchema,
  mergeRequestSchema,
  repoConnectionCreateSchema,
  repoConnectionSchema,
  taskSpecSchema,
} from '../schemas/codegen'

export type TaskSpec = z.infer<typeof taskSpecSchema>
export type ChangesetPrompt = z.infer<typeof changesetPromptSchema>
export type Changeset = z.infer<typeof changesetSchema>
export type MergeRequest = z.infer<typeof mergeRequestSchema>
export type RepoConnection = z.infer<typeof repoConnectionSchema>
export type RepoConnectionCreate = z.infer<typeof repoConnectionCreateSchema>
export type AccessibleRepo = z.infer<typeof accessibleRepoSchema>
