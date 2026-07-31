import type { PublicationAuthorization } from '@/api/types/codegen'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

type TenantAuthorization = Extract<
  PublicationAuthorization,
  { schema_version: 'tenant_publication_authorization@1' }
>

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

function Assignment({
  assignment,
}: {
  assignment: TenantAuthorization['request']['execution_snapshot']['assignments'][number]
}) {
  return (
    <div className="rounded-md border p-3">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label(assignment.role)} model assignment
      </p>
      <p className="mt-1 text-sm">
        <code className="font-mono text-xs break-all">
          {assignment.provider}/{assignment.model_id}
        </code>
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        Assignment {assignment.assignment_version} · connection {assignment.connection_version} ·
        inventory {assignment.inventory_version}
      </p>
    </div>
  )
}

function TenantPublicationCard({ authorization }: { authorization: TenantAuthorization }) {
  const { decision, request } = authorization
  const { execution_snapshot: snapshot, runtime_identity: runtime } = request

  return (
    <Card>
      <CardHeader>
        <CardTitle>Publication authorization</CardTitle>
        <CardDescription>
          The project&apos;s immutable model assignments and the attested Codegen runtime authorize
          this draft pull request. GitHub remains authoritative for CI, review policy, and merge.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Fact title="Decision">
            <span className="inline-flex rounded-full border border-emerald-300 bg-emerald-50 px-2 py-0.5 text-xs font-medium text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300">
              Allowed
            </span>
          </Fact>
          <Fact title="Publication stage">{label(request.requested_stage)}</Fact>
          <Fact title="Risk">{label(request.risk)}</Fact>
          <Fact title="Draft only">{authorization.draft_only ? 'Yes' : 'No'}</Fact>
          <Fact title="Authority">Tenant model assignments</Fact>
          <Fact title="Project">
            <code className="font-mono text-xs">{snapshot.project_id}</code>
          </Fact>
          <Fact title="Repository">
            <code className="font-mono text-xs">{snapshot.repository_full_name}</code>
          </Fact>
          <Fact title="Repository grant">
            <code className="font-mono text-xs break-all">{snapshot.repository_grant_id}</code>
          </Fact>
          <Fact title="Codegen revision">
            <code className="font-mono text-xs break-all">{snapshot.codegen_revision}</code>
          </Fact>
          <Fact title="Egress transport">{label(runtime.egress_transport)}</Fact>
          <Fact title="Max concurrent jobs">{runtime.max_concurrent_jobs}</Fact>
          <Fact title="Ready for review">{decision.ready_for_review ? 'Yes' : 'No'}</Fact>
          <Fact title="Publish branch">{decision.publish_branch ? 'Granted' : 'Not granted'}</Fact>
          <Fact title="Create pull request">
            {decision.create_pull_request ? 'Granted' : 'Not granted'}
          </Fact>
        </div>

        <div className="grid gap-3 md:grid-cols-2">
          {snapshot.assignments.map((assignment) => (
            <Assignment key={assignment.role} assignment={assignment} />
          ))}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Digest title="Execution snapshot SHA-256" value={request.execution_snapshot_sha256} />
          <Digest title="Runtime identity SHA-256" value={runtime.identity_sha256} />
          <Digest
            title="Behavior configuration SHA-256"
            value={runtime.behavior_configuration_sha256}
          />
          <Digest title="Egress policy SHA-256" value={runtime.egress_policy_sha256} />
          <Digest title="Controller image ID" value={runtime.controller_image_id} />
          <Digest title="Worker image ID" value={runtime.worker_image_id} />
          <Digest title="Egress proxy image ID" value={runtime.egress_proxy_image_id} />
          <Digest title="Publication decision SHA-256" value={decision.decision_sha256} />
          <Digest title="Authorization SHA-256" value={authorization.authorization_sha256} />
        </div>
      </CardContent>
    </Card>
  )
}

export function PublicationAuthorizationCard({
  authorization,
}: {
  authorization: PublicationAuthorization
}) {
  if (authorization.schema_version === 'tenant_publication_authorization@1') {
    return <TenantPublicationCard authorization={authorization} />
  }

  const { decision, request } = authorization
  return (
    <Card>
      <CardHeader>
        <CardTitle>Publication authorization</CardTitle>
        <CardDescription>
          Local development authorization. Pull requests are always drafts and this does not claim
          tenant model-assignment or production runtime authority. GitHub remains authoritative for
          CI, review policy, and merge.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <Fact title="Decision">Allowed</Fact>
          <Fact title="Publication stage">{label(request.requested_stage)}</Fact>
          <Fact title="Risk">{label(request.risk)}</Fact>
          <Fact title="Authority">Local development only</Fact>
          <Fact title="Model">
            <code className="font-mono text-xs break-all">{request.model}</code>
          </Fact>
          <Fact title="Codegen revision">
            <code className="font-mono text-xs break-all">{request.codegen_revision}</code>
          </Fact>
          <Fact title="Draft only">{authorization.draft_only ? 'Yes' : 'No'}</Fact>
          <Fact title="Ready for review">{decision.ready_for_review ? 'Yes' : 'No'}</Fact>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          <Digest title="Publication decision SHA-256" value={decision.decision_sha256} />
          <Digest title="Authorization SHA-256" value={authorization.authorization_sha256} />
        </div>
      </CardContent>
    </Card>
  )
}
