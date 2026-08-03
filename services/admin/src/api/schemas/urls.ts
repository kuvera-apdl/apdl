import { z } from 'zod'

/**
 * URL contract for server-provided links rendered as external anchors.
 *
 * These links can target several server-selected external services, so this
 * shared contract validates transport and URL safety rather than one host.
 * GitHub-specific authorization redirects are separately restricted to the
 * canonical github.com origin by the Admin callback relay.
 */
export const externalHttpsUrlSchema = z
  .string()
  .min(1)
  .max(2048)
  .url()
  .superRefine((value, context) => {
    let url: URL
    try {
      url = new URL(value)
    } catch {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'external URL must be valid',
      })
      return
    }
    if (url.protocol !== 'https:') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'external URL must use HTTPS',
      })
    }
    if (url.username !== '' || url.password !== '') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'external URL must not contain credentials',
      })
    }
    if (url.hash !== '') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'external URL must not contain a fragment',
      })
    }
  })
