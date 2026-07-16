import { ExternalLink } from 'lucide-react'

import type {
  Changeset,
  RuntimeAcceptancePlan,
  RuntimeEvidenceAssessment,
  RuntimeEvidenceObservation,
} from '@/api/types/codegen'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { ExternalCIStatusPill } from '@/features/codegen/ChangesetStatusPill'

function label(value: string): string {
  return value.replace(/_/g, ' ')
}

function HeadLink({ repository, sha }: { repository: string; sha: string }) {
  return (
    <a
      href={`https://github.com/${repository}/commit/${encodeURIComponent(sha)}`}
      target="_blank"
      rel="noreferrer"
      className="inline-flex items-center gap-1 underline"
      title={sha}
    >
      <code className="font-mono text-xs">{sha.slice(0, 12)}</code>
      <ExternalLink className="h-3 w-3" />
    </a>
  )
}

function EvidenceAssessment({ assessment }: { assessment: RuntimeEvidenceAssessment }) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">Runtime requirement evidence</span>
        <span className="text-muted-foreground">
          exact head <code className="font-mono text-xs">{assessment.head_sha.slice(0, 12)}</code>
        </span>
        <span className="text-muted-foreground">GitHub CI at collection:</span>
        <ExternalCIStatusPill status={assessment.external_ci_status} />
      </div>
      {assessment.requirements.length === 0 ? (
        <p className="text-xs text-muted-foreground">No runtime requirements were assessed.</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {assessment.requirements.map((requirement) => (
            <li key={requirement.requirement_id} className="rounded bg-muted/60 p-2">
              <code className="font-mono">{requirement.requirement_id}</code> ·{' '}
              {label(requirement.status)}
              {requirement.artifact_names.length > 0
                ? ` — ${requirement.artifact_names.join(', ')}`
                : requirement.reason
                  ? ` — ${requirement.reason}`
                  : ''}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function RuntimeAcceptancePlanCard({ plan }: { plan: RuntimeAcceptancePlan }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Runtime acceptance plan</CardTitle>
        <CardDescription>
          Planned runtime checks and required GitHub Actions artifacts. APDL records this plan;
          GitHub executes checks and remains authoritative for CI and merge.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Checks</p>
            <p className="text-sm">{plan.checks.length}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Blockers</p>
            <p className="text-sm">{plan.blockers.length}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Repository</p>
            <p className="text-sm font-mono break-all">{plan.repo ?? '—'}</p>
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Branch</p>
            <p className="text-sm font-mono break-all">{plan.branch ?? '—'}</p>
          </div>
        </div>

        {plan.checks.map((check) => (
          <div key={check.check_id} className="rounded-md border p-3 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <code className="font-mono text-xs">{check.check_id}</code>
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                {label(check.surface)}
              </span>
              <span className="text-xs text-muted-foreground">
                {check.requirement_ids.join(', ')}
              </span>
            </div>
            <p className="mt-2 text-xs">
              <code className="font-mono">{check.command.command}</code>{' '}
              <span className="text-muted-foreground">in {check.command.cwd}</span>
            </p>
            <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
              {check.expected_artifacts.map((artifact) => (
                <li key={artifact.artifact_name}>
                  Artifact <code className="font-mono">{artifact.artifact_name}</code> ·{' '}
                  {label(artifact.evidence_kind)} · {artifact.paths.join(', ')}
                </li>
              ))}
            </ul>
          </div>
        ))}

        {plan.blockers.length > 0 ? (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Runtime blockers
            </p>
            {plan.blockers.map((blocker) => (
              <div
                key={`${blocker.requirement_id}:${blocker.surface}`}
                className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-800 dark:bg-amber-950/30"
              >
                <code className="font-mono text-xs">{blocker.requirement_id}</code> ·{' '}
                {label(blocker.surface)} — {blocker.reason}
              </div>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  )
}

function RuntimeObservation({ observation }: { observation: RuntimeEvidenceObservation }) {
  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">GitHub Actions evidence</span>
        <ExternalCIStatusPill status={observation.assessment.external_ci_status} />
        <span className="text-xs text-muted-foreground">
          observed <RelativeTime value={observation.observed_at} />
        </span>
        <span className="ml-auto text-xs">
          exact head <HeadLink repository={observation.repository} sha={observation.head_sha} />
        </span>
      </div>

      <div className="mt-3">
        <EvidenceAssessment assessment={observation.assessment} />
      </div>

      {observation.artifacts.length > 0 ? (
        <div className="mt-3 space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Artifacts</p>
          {observation.artifacts.map((artifact) => (
            <div
              key={`${artifact.workflow_run_id}:${artifact.artifact_id ?? 0}:${artifact.artifact_name}`}
              className="rounded bg-muted/60 p-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-2">
                {artifact.github_url ? (
                  <a
                    href={artifact.github_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 font-medium underline"
                  >
                    {artifact.artifact_name}
                    <ExternalLink className="h-3 w-3" />
                  </a>
                ) : (
                  <span className="font-medium">{artifact.artifact_name}</span>
                )}
                <span>{label(artifact.status)}</span>
                <span className="text-muted-foreground">
                  {artifact.requirement_ids.join(', ')}
                </span>
              </div>
              {artifact.unverified_reason ? (
                <p className="mt-1 text-muted-foreground">{artifact.unverified_reason}</p>
              ) : null}
              {artifact.files.length > 0 ? (
                <ul className="mt-1 list-disc pl-4 text-muted-foreground">
                  {artifact.files.map((file) => (
                    <li key={file.path}>
                      <code className="font-mono">{file.path}</code> · {file.byte_count} bytes
                      {file.redacted ? ' · redacted' : ''}
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      {observation.job_logs.length > 0 ? (
        <div className="mt-3 space-y-2">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Bounded job-log excerpts
          </p>
          {observation.job_logs.map((job) => (
            <details key={`${job.workflow_run_id}:${job.job_id}`} className="rounded bg-muted/60 p-2 text-xs">
              <summary className="cursor-pointer font-medium">
                {job.job_name} · {job.excerpt_byte_count}/{job.source_byte_count} bytes
                {job.truncated ? ' · truncated' : ''}
                {job.redacted ? ' · redacted' : ''}
              </summary>
              <a
                href={job.github_url}
                target="_blank"
                rel="noreferrer"
                className="mt-2 inline-flex items-center gap-1 underline"
              >
                Open job on GitHub
                <ExternalLink className="h-3 w-3" />
              </a>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-background p-2">
                {job.text_excerpt}
              </pre>
            </details>
          ))}
        </div>
      ) : null}

      {observation.collection_errors.length > 0 ? (
        <div className="mt-3 rounded border border-amber-300 bg-amber-50 p-2 text-xs dark:border-amber-800 dark:bg-amber-950/30">
          <p className="font-medium">Collection diagnostics</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {observation.collection_errors.map((error) => <li key={error}>{error}</li>)}
          </ul>
        </div>
      ) : null}
    </div>
  )
}

export function RuntimeEvidenceHistory({
  observations,
  currentAssessment,
  externalCIStatus,
}: {
  observations: RuntimeEvidenceObservation[]
  currentAssessment: RuntimeEvidenceAssessment | null
  externalCIStatus: Changeset['external_ci_status']
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Runtime acceptance evidence</CardTitle>
        <CardDescription>
          Read-only exact-head GitHub Actions artifacts, log excerpts, and collection diagnostics.
          Runtime evidence never promotes or replaces GitHub&apos;s external CI status.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">Current GitHub-owned external CI:</span>
          {externalCIStatus ? <ExternalCIStatusPill status={externalCIStatus} /> : <span>—</span>}
        </div>
        {currentAssessment ? (
          <div className="rounded-md border p-3">
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Current exact-head projection
            </p>
            <EvidenceAssessment assessment={currentAssessment} />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No runtime evidence assessment is projected for the current PR head.
          </p>
        )}

        {observations.length === 0 ? (
          <p className="text-sm text-muted-foreground">No runtime evidence observations recorded yet.</p>
        ) : (
          <div className="space-y-3">
            {observations.map((observation) => (
              <RuntimeObservation key={observation.observation_id} observation={observation} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
