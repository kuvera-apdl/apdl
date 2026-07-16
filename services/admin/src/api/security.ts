import { z } from 'zod'

import { request } from '@/api/http'

const securityNotificationSchema = z
  .object({
    notification_id: z.string().uuid(),
    kind: z.literal('suspicious_login_activity'),
    status: z.literal('unread'),
    observed_failures: z.number().int().positive(),
    window_started_at: z.string().datetime({ offset: true }),
    last_detected_at: z.string().datetime({ offset: true }),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict()

const securityNotificationsSchema = z.array(securityNotificationSchema)

export type SecurityNotification = z.infer<typeof securityNotificationSchema>

const authConnection = { baseUrl: '', actor: '' }

export function listSecurityNotifications(): Promise<SecurityNotification[]> {
  return request(authConnection, '/api/auth/security-notifications', {
    schema: securityNotificationsSchema,
  })
}

export function acknowledgeSecurityNotification(notificationId: string): Promise<unknown> {
  return request(
    authConnection,
    `/api/auth/security-notifications/${encodeURIComponent(notificationId)}/acknowledge`,
    { method: 'POST' },
  )
}
