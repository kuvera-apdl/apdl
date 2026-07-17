import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ShieldAlert } from 'lucide-react'

import {
  acknowledgeSecurityNotification,
  listSecurityNotifications,
} from '@/api/security'
import { Button } from '@/components/ui/button'

const SECURITY_NOTIFICATIONS_KEY = ['auth', 'security-notifications'] as const

export function SecurityNotificationBanner() {
  const queryClient = useQueryClient()
  const notifications = useQuery({
    queryKey: SECURITY_NOTIFICATIONS_KEY,
    queryFn: listSecurityNotifications,
    staleTime: 60_000,
  })
  const acknowledge = useMutation({
    mutationFn: acknowledgeSecurityNotification,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: SECURITY_NOTIFICATIONS_KEY })
    },
  })
  const notification = notifications.data?.[0]
  if (!notification) return null

  return (
    <div
      role="alert"
      className="flex flex-col gap-3 rounded-md border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between"
    >
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
        <div>
          <p className="font-medium">Suspicious sign-in activity detected</p>
          <p className="mt-1 text-amber-900/80 dark:text-amber-100/80">
            We observed {notification.observed_failures} failed attempts during a recent risk
            window. Your account remains accessible with the correct password.
          </p>
        </div>
      </div>
      <Button
        type="button"
        variant="outline"
        size="sm"
        disabled={acknowledge.isPending}
        onClick={() => acknowledge.mutate(notification.notification_id)}
      >
        Acknowledge
      </Button>
    </div>
  )
}
