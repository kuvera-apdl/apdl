import type { PublicationAuthorization } from '@/api/types/codegen'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

function label(value: string): string {
  return value
    .split('_')
    .map((word) => (word === 'pr' ? 'PR' : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(' ')
}

function Fact({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{title}</p>
      <div className="text-sm">{children}</div>
    </div>
  )
}

function Digest({ title, value }: { title: string; value: string }) {
  return (
    <Fact title={title}>
      <code className="font-mono text-xs break-all">{value}</code>
    </Fact>
  )
}

export function PublicationAuthorizationCard({
  authorization,
}: {
  authorization: PublicationAuthorization
}) {
  const { decision, request } = authorization

  return (
    <Card className={cn(!decision.allowed && 'border-amber-400 dark:border-amber-800')}>
      <CardHeader>
        <CardTitle>Publication authorization</CardTitle>
        <CardDescription>
          Read-only rollout evidence evaluated before APDL may publish a branch and pull request.
          GitHub remains authoritative for CI, review policy, and merge.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Fact title="Decision">
            <span
              className={cn(
                'inline-flex rounded-full border px-2 py-0.5 text-xs font-medium',
                decision.allowed
                  ? 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300'
                  : 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300',
              )}
            >
              {decision.allowed ? 'Allowed' : 'Denied'}
            </span>
          </Fact>
          <Fact title="Rollout stage">{label(request.requested_stage)}</Fact>
          <Fact title="Risk">{label(request.risk)}</Fact>
          <Fact title="Ready for review">{decision.ready_for_review ? 'Yes' : 'No'}</Fact>
          <Fact title="Model">
            <code className="font-mono text-xs break-all">{request.model}</code>
          </Fact>
          <Fact title="Codegen revision">
            <code className="font-mono text-xs break-all">{request.codegen_revision}</code>
          </Fact>
          <Fact title="Publish branch">{decision.publish_branch ? 'Granted' : 'Not granted'}</Fact>
          <Fact title="Create pull request">
            {decision.create_pull_request ? 'Granted' : 'Not granted'}
          </Fact>
        </div>

        {!decision.allowed ? (
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Denial reasons
            </p>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
              {decision.reasons.map((reason, index) => (
                <li key={`${index}:${reason}`}>{reason}</li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Digest title="Evaluation report SHA-256" value={authorization.report_sha256} />
          <Digest title="Evidence bundle SHA-256" value={authorization.bundle_sha256} />
          <Digest title="Rollout policy SHA-256" value={authorization.policy_sha256} />
          <Digest title="Rollout decision SHA-256" value={decision.decision_sha256} />
          <Digest title="Authorization SHA-256" value={authorization.authorization_sha256} />
        </div>
      </CardContent>
    </Card>
  )
}
