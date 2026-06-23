import type { z } from 'zod'

import type {
  changesetSchema,
  mergeRequestSchema,
  taskSpecSchema,
} from '../schemas/codegen'

export type TaskSpec = z.infer<typeof taskSpecSchema>
export type Changeset = z.infer<typeof changesetSchema>
export type MergeRequest = z.infer<typeof mergeRequestSchema>
