import { z } from 'zod'

/**
 * URL contract for server-provided links rendered as external anchors.
 *
 * GitHub Enterprise installations may use operator-owned hosts, so the client
 * cannot hard-code github.com. It can still reject active schemes, plaintext
 * transport, embedded credentials, fragments that obscure the destination,
 * and unbounded values before a link reaches the DOM.
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
