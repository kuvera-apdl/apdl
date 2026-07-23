import { Loader2, UserPlus } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, Navigate, useNavigate } from 'react-router-dom'
import { z } from 'zod'

import { ApiError } from '@/api/http'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/core/auth'
import { useAuthCapabilities } from '@/features/auth/hooks'

const registrationSchema = z
  .object({
    email: z.string().email('Enter a valid email address'),
    password: z.string().min(12, 'Use at least 12 characters').max(1024),
    confirmation: z.string(),
  })
  .strict()
  .refine((value) => value.password === value.confirmation, {
    message: 'Passwords do not match',
    path: ['confirmation'],
  })

export function RegisterPage() {
  const { authenticated, initializing, register } = useAuth()
  const capabilities = useAuthCapabilities()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [serverBlock, setServerBlock] = useState<
    'account_capacity_reached' | 'registration_disabled' | null
  >(null)

  if (!initializing && authenticated) return <Navigate to="/" replace />

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    const parsed = registrationSchema.safeParse({ email, password, confirmation })
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? 'Invalid registration')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await register(parsed.data.email, parsed.data.password)
      navigate('/settings/workspace', { replace: true })
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'account_exists') {
        setError('An account already exists for this email. Sign in instead.')
      } else if (caught instanceof ApiError && caught.code === 'account_capacity_reached') {
        setServerBlock('account_capacity_reached')
      } else if (caught instanceof ApiError && caught.code === 'registration_disabled') {
        setServerBlock('registration_disabled')
      } else if (caught instanceof ApiError && caught.status === 429) {
        setError('Too many attempts. Wait a minute and try again.')
      } else {
        setError('The admin service is unavailable. Try again shortly.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  const registrationState = serverBlock ?? (
    capabilities.isSuccess && !capabilities.data.registration_enabled
      ? 'registration_disabled'
      : null
  )

  if (initializing || capabilities.isPending) {
    return (
      <RegistrationUnavailable
        title="Checking registration availability"
        description="Verifying whether this APDL deployment is accepting new accounts."
        status
      />
    )
  }

  if (capabilities.isError) {
    return (
      <RegistrationUnavailable
        title="Unable to verify registration availability"
        description="Registration is closed until the admin service can confirm that new accounts are enabled."
        onRetry={() => void capabilities.refetch()}
      />
    )
  }

  if (registrationState === 'registration_disabled') {
    return (
      <RegistrationUnavailable
        title="Registration is disabled"
        description="This APDL deployment is not accepting new accounts. Ask an operator for access."
      />
    )
  }

  if (registrationState === 'account_capacity_reached') {
    return (
      <RegistrationUnavailable
        title="Account capacity reached"
        description="This APDL deployment has reached its account limit. Ask an operator for access."
      />
    )
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <UserPlus className="h-5 w-5" />
          </div>
          <div>
            <CardTitle>Create your APDL account</CardTitle>
            <CardDescription className="mt-1.5">
              Register with an email and password. New accounts start with no project access.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={(event) => void onSubmit(event)} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="register-email">Email</Label>
              <Input
                id="register-email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={submitting}
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="new-password">Password</Label>
              <Input
                id="new-password"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={submitting}
              />
              <p className="text-xs text-muted-foreground">At least 12 characters.</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="confirm-password">Confirm password</Label>
              <Input
                id="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                disabled={submitting}
              />
            </div>
            {error ? <p className="text-sm text-destructive">{error}</p> : null}
            <Button className="w-full" type="submit" disabled={submitting || initializing}>
              {submitting ? <Loader2 className="animate-spin" /> : null}
              Create account
            </Button>
          </form>
          <p className="mt-5 text-center text-sm text-muted-foreground">
            Already registered?{' '}
            <Link className="font-medium text-primary underline-offset-4 hover:underline" to="/login">
              Sign in
            </Link>
          </p>
        </CardContent>
      </Card>
    </main>
  )
}

function RegistrationUnavailable({
  title,
  description,
  onRetry,
  status = false,
}: {
  title: string
  description: string
  onRetry?: () => void
  status?: boolean
}) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <UserPlus className="h-5 w-5" />
          </div>
          <div>
            <CardTitle>{title}</CardTitle>
            <CardDescription className="mt-1.5" role={status ? 'status' : undefined}>
              {description}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {onRetry ? (
            <Button type="button" className="w-full" onClick={onRetry}>
              Retry
            </Button>
          ) : null}
          <p className="text-center text-sm text-muted-foreground">
            Already registered?{' '}
            <Link className="font-medium text-primary underline-offset-4 hover:underline" to="/login">
              Sign in
            </Link>
          </p>
        </CardContent>
      </Card>
    </main>
  )
}
