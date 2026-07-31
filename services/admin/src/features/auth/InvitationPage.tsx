import { useQuery } from '@tanstack/react-query'
import { Loader2, MailCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { z } from 'zod'

import { ApiError } from '@/api/http'
import { inspectProjectInvitation } from '@/api/members'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/core/auth'
import { useWorkspace } from '@/core/workspace'

const passwordSchema = z
  .object({
    password: z.string().min(12, 'Use at least 12 characters').max(1024),
    confirmation: z.string(),
  })
  .strict()
  .refine((value) => value.password === value.confirmation, {
    message: 'Passwords do not match',
    path: ['confirmation'],
  })

function invitationError(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return 'Too many invitation attempts. Wait a minute and try again.'
  }
  if (error instanceof ApiError && error.status === 409) {
    return error.message
  }
  return 'This invitation is invalid, expired, revoked, or already accepted.'
}

export function InvitationPage() {
  const { token = '' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const { authenticated, identity, initializing, acceptInvitation, registerInvitation, logout } =
    useAuth()
  const { setActive } = useWorkspace()
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const invitation = useQuery({
    queryKey: ['admin', 'invitation', token],
    queryFn: ({ signal }) => inspectProjectInvitation(token, signal),
    enabled: token.length > 0,
    retry: false,
  })

  const complete = (projectId: string) => {
    setActive(projectId)
    navigate('/', { replace: true })
  }

  const accept = async () => {
    if (!invitation.data) return
    const projectId = invitation.data.project_id
    setSubmitting(true)
    setError(null)
    try {
      await acceptInvitation(token)
      complete(projectId)
    } catch (caught) {
      setError(invitationError(caught))
      if (caught instanceof ApiError && caught.status === 404) void invitation.refetch()
    } finally {
      setSubmitting(false)
    }
  }

  const register = async (event: FormEvent) => {
    event.preventDefault()
    const parsed = passwordSchema.safeParse({ password, confirmation })
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? 'Invalid password')
      return
    }
    if (!invitation.data) return
    const projectId = invitation.data.project_id
    setSubmitting(true)
    setError(null)
    try {
      await registerInvitation(token, parsed.data.password)
      complete(projectId)
    } catch (caught) {
      setError(invitationError(caught))
    } finally {
      setSubmitting(false)
    }
  }

  const switchAccount = async () => {
    setSubmitting(true)
    try {
      await logout()
      navigate('/login', {
        replace: true,
        state: { from: `${location.pathname}${location.search}` },
      })
    } finally {
      setSubmitting(false)
    }
  }

  if (invitation.isPending || initializing) {
    return (
      <InvitationFrame
        title="Checking your invitation"
        description="Validating the secure invitation and its current project authority."
        loading
      />
    )
  }

  if (invitation.isError || !invitation.data) {
    return (
      <InvitationFrame
        title="Invitation unavailable"
        description={invitationError(invitation.error)}
      />
    )
  }

  const matchesSignedInAccount =
    authenticated && identity?.email.toLowerCase() === invitation.data.email.toLowerCase()

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-lg">
        <CardHeader className="space-y-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <MailCheck className="h-5 w-5" />
          </div>
          <div>
            <CardTitle>Join project {invitation.data.project_id}</CardTitle>
            <CardDescription className="mt-1.5">
              This invitation is restricted to {invitation.data.email}.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="rounded-md border p-3">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Granted roles
            </p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              {invitation.data.roles.map((role) => (
                <Badge key={role} variant="secondary" className="font-mono text-xs">
                  {role}
                </Badge>
              ))}
            </div>
          </div>

          {matchesSignedInAccount ? (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground">
                Signed in as {identity?.email}. Accept to add this project to your workspace.
              </p>
              <Button className="w-full" disabled={submitting} onClick={() => void accept()}>
                {submitting ? <Loader2 className="animate-spin" /> : null}
                Accept invitation
              </Button>
            </div>
          ) : authenticated ? (
            <div className="space-y-3">
              <p className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                You are signed in as {identity?.email}, but this invitation belongs to{' '}
                {invitation.data.email}.
              </p>
              <Button
                className="w-full"
                variant="outline"
                disabled={submitting}
                onClick={() => void switchAccount()}
              >
                Sign out and switch account
              </Button>
            </div>
          ) : (
            <>
              <div className="rounded-md border p-3 text-sm">
                Already have an APDL account?{' '}
                <Link
                  className="font-medium text-primary underline-offset-4 hover:underline"
                  to="/login"
                  state={{ from: `${location.pathname}${location.search}` }}
                >
                  Sign in to accept
                </Link>
                .
              </div>
              <form onSubmit={(event) => void register(event)} className="space-y-4" noValidate>
                <div>
                  <p className="font-medium">Create your invited account</p>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Invitation registration works even when public registration is disabled.
                  </p>
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="invitation-password">Password</Label>
                  <Input
                    id="invitation-password"
                    type="password"
                    autoComplete="new-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    disabled={submitting}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="invitation-confirmation">Confirm password</Label>
                  <Input
                    id="invitation-confirmation"
                    type="password"
                    autoComplete="new-password"
                    value={confirmation}
                    onChange={(event) => setConfirmation(event.target.value)}
                    disabled={submitting}
                  />
                </div>
                <Button className="w-full" type="submit" disabled={submitting}>
                  {submitting ? <Loader2 className="animate-spin" /> : null}
                  Create account and accept
                </Button>
              </form>
            </>
          )}
          {error ? (
            <p className="text-sm text-destructive" role="alert">
              {error}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </main>
  )
}

function InvitationFrame({
  title,
  description,
  loading = false,
}: {
  title: string
  description: string
  loading?: boolean
}) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            {loading ? <Loader2 className="h-5 w-5 animate-spin" /> : <MailCheck className="h-5 w-5" />}
          </div>
          <div>
            <CardTitle>{title}</CardTitle>
            <CardDescription className="mt-1.5" role={loading ? 'status' : 'alert'}>
              {description}
            </CardDescription>
          </div>
        </CardHeader>
        {!loading ? (
          <CardContent>
            <Link
              className="text-sm font-medium text-primary underline-offset-4 hover:underline"
              to="/login"
            >
              Return to sign in
            </Link>
          </CardContent>
        ) : null}
      </Card>
    </main>
  )
}
