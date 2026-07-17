import { Loader2, LockKeyhole } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { z } from 'zod'

import { ApiError } from '@/api/http'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/core/auth'

const loginSchema = z
  .object({
    email: z.string().email('Enter a valid email address'),
    password: z.string().min(1, 'Enter your password'),
  })
  .strict()

function safeReturnPath(value: unknown): string {
  return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//')
    ? value
    : '/'
}

export function LoginPage() {
  const { authenticated, initializing, login, logoutReason } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [retryAfterSeconds, setRetryAfterSeconds] = useState<number | null>(null)

  useEffect(() => {
    if (retryAfterSeconds === null) return
    const timer = window.setTimeout(() => {
      setRetryAfterSeconds((current) => {
        if (current === null || current <= 1) return null
        return current - 1
      })
    }, 1_000)
    return () => window.clearTimeout(timer)
  }, [retryAfterSeconds])

  if (!initializing && authenticated) return <Navigate to="/" replace />

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    if (retryAfterSeconds !== null) return
    const parsed = loginSchema.safeParse({ email, password })
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? 'Invalid login')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await login(parsed.data.email, parsed.data.password)
      const state = location.state as { from?: unknown } | null
      navigate(safeReturnPath(state?.from), { replace: true })
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) {
        setError('Invalid email or password.')
      } else if (
        caught instanceof ApiError &&
        caught.status === 429 &&
        caught.code === 'auth_throttled' &&
        caught.retryAfterSeconds !== null
      ) {
        setRetryAfterSeconds(caught.retryAfterSeconds)
      } else {
        setError('The admin service is unavailable. Try again shortly.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <LockKeyhole className="h-5 w-5" />
          </div>
          <div>
            <CardTitle>Sign in to APDL Admin</CardTitle>
            <CardDescription className="mt-1.5">
              Use your administrator account. Service credentials remain on the server.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent>
          {logoutReason === 'unauthorized' ? (
            <p className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              Your session expired or was revoked. Sign in again.
            </p>
          ) : null}
          <form onSubmit={(event) => void onSubmit(event)} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                autoComplete="username"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                disabled={submitting}
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                disabled={submitting}
              />
            </div>
            {retryAfterSeconds !== null ? (
              <p className="text-sm text-destructive" role="alert">
                Too many attempts from this browser or network. Try again in{' '}
                {retryAfterSeconds} {retryAfterSeconds === 1 ? 'second' : 'seconds'}.
              </p>
            ) : error ? (
              <p className="text-sm text-destructive">{error}</p>
            ) : null}
            <Button
              className="w-full"
              type="submit"
              disabled={submitting || initializing || retryAfterSeconds !== null}
            >
              {submitting ? <Loader2 className="animate-spin" /> : null}
              Sign in
            </Button>
          </form>
          <p className="mt-5 text-center text-sm text-muted-foreground">
            Have an invitation?{' '}
            <Link className="font-medium text-primary underline-offset-4 hover:underline" to="/register">
              Create your account
            </Link>
          </p>
        </CardContent>
      </Card>
    </main>
  )
}
