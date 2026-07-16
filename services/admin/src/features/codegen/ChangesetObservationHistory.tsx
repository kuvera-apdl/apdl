import { ExternalLink } from 'lucide-react'

import type { ChangesetObservationHistory as ObservationHistory } from '@/api/types/codegen'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  ExternalCIStatusPill,
  GitHubPRStatusPill,
} from '@/features/codegen/ChangesetStatusPill'

function label(value: string): string {
  return value.replace(/_/g, ' ')
}

function commitUrl(repository: string, sha: string): string {
  return `https://github.com/${repository}/commit/${encodeURIComponent(sha)}`
}

function HeadLink({ repository, sha }: { repository: string; sha: string }) {
  return (
    <a
      href={commitUrl(repository, sha)}
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

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </h3>
  )
}

export function ChangesetObservationHistory({ history }: { history: ObservationHistory }) {
  const empty =
    history.pull_requests.length === 0 &&
    history.ci_verifications.length === 0 &&
    history.remediation_attempts.length === 0

  return (
    <Card>
      <CardHeader>
        <CardTitle>GitHub observation history</CardTitle>
        <CardDescription>
          Read-only append-only PR, exact-head CI, and remediation evidence observed from GitHub.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {empty ? (
          <p className="text-sm text-muted-foreground">No GitHub observations recorded yet.</p>
        ) : null}

        {history.pull_requests.length > 0 ? (
          <section className="space-y-2">
            <SectionTitle>Pull request events</SectionTitle>
            {history.pull_requests.map((observation) => (
              <div key={observation.observation_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <GitHubPRStatusPill status={observation.status} />
                  <span>{label(observation.action)}</span>
                  <span className="text-muted-foreground">
                    observed <RelativeTime value={observation.observed_at} />
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
                  <a
                    href={observation.github_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 underline"
                  >
                    PR #{observation.pr_number}
                    <ExternalLink className="h-3 w-3" />
                  </a>
                  <span>
                    exact head <HeadLink repository={observation.repository} sha={observation.head_sha} />
                  </span>
                  {observation.merge_sha ? (
                    <span>
                      merge <HeadLink repository={observation.repository} sha={observation.merge_sha} />
                    </span>
                  ) : null}
                </div>
              </div>
            ))}
          </section>
        ) : null}

        {history.ci_verifications.length > 0 ? (
          <section className="space-y-2">
            <SectionTitle>External CI observations</SectionTitle>
            {history.ci_verifications.map((observation) => (
              <div key={observation.observation_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <ExternalCIStatusPill status={observation.status} />
                  <span className="text-muted-foreground">
                    {observation.signals.length} signal{observation.signals.length === 1 ? '' : 's'} ·{' '}
                    observed <RelativeTime value={observation.observed_at} />
                  </span>
                  <span className="ml-auto text-xs">
                    exact head <HeadLink repository={observation.repository} sha={observation.head_sha} />
                  </span>
                </div>
                {observation.status === 'unverified_external_ci' ? (
                  <p className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-amber-900 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-200">
                    No CI signals were configured or observed for this head. It remains unverified;
                    this is never treated as passed.
                  </p>
                ) : null}
                {observation.signals.length > 0 ? (
                  <ul className="mt-3 space-y-2">
                    {observation.signals.map((signal) => (
                      <li key={signal.signal_id} className="rounded bg-muted/60 p-2">
                        <div className="flex flex-wrap items-center gap-2">
                          {signal.github_url ? (
                            <a
                              href={signal.github_url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-flex items-center gap-1 font-medium underline"
                            >
                              {signal.name}
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          ) : (
                            <span className="font-medium">{signal.name}</span>
                          )}
                          <span className="text-xs text-muted-foreground">
                            {label(signal.kind)} · {signal.conclusion}
                          </span>
                        </div>
                        {signal.summary ? (
                          <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">
                            {signal.summary}
                          </p>
                        ) : null}
                        {signal.annotations.length > 0 ? (
                          <ul className="mt-2 list-disc space-y-1 pl-4 text-xs">
                            {signal.annotations.map((annotation, index) => (
                              <li key={`${annotation.path}:${annotation.start_line ?? 0}:${index}`}>
                                <code className="font-mono">{annotation.path}</code>
                                {annotation.start_line ? `:${annotation.start_line}` : ''} —{' '}
                                {annotation.message}
                              </li>
                            ))}
                          </ul>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {observation.requirement_results.length > 0 ? (
                  <ul className="mt-3 space-y-1 text-xs">
                    {observation.requirement_results.map((result) => (
                      <li key={result.evidence_id}>
                        <code className="font-mono">{result.requirement_id}</code> · {result.status} —{' '}
                        {result.explanation}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {observation.failure_summary ? (
                  <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-2 text-xs">
                    {observation.failure_summary}
                  </pre>
                ) : null}
              </div>
            ))}
          </section>
        ) : null}

        {history.remediation_attempts.length > 0 ? (
          <section className="space-y-2">
            <SectionTitle>Remediation events</SectionTitle>
            {history.remediation_attempts.map((attempt) => (
              <div key={attempt.event_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                    {label(attempt.disposition)}
                  </span>
                  <span>
                    attempt {attempt.attempt_number}, event {attempt.event_sequence}
                  </span>
                  <span className="text-xs text-muted-foreground">
                    {label(attempt.classification)} · {Math.round(attempt.confidence * 100)}% confidence
                  </span>
                  <span className="ml-auto text-xs text-muted-foreground">
                    recorded <RelativeTime value={attempt.recorded_at} />
                  </span>
                </div>
                <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs">
                  <span>
                    failed head <HeadLink repository={attempt.repository} sha={attempt.failed_head_sha} />
                  </span>
                  {attempt.resulting_commit_sha ? (
                    <span>
                      resulting commit{' '}
                      <HeadLink repository={attempt.repository} sha={attempt.resulting_commit_sha} />
                    </span>
                  ) : null}
                </div>
                {attempt.changed_files.length > 0 ? (
                  <p className="mt-2 text-xs text-muted-foreground">
                    Changed files: {attempt.changed_files.join(', ')}
                  </p>
                ) : null}
                {attempt.prompt_evidence.length > 0 ? (
                  <div className="mt-2 space-y-1 text-xs">
                    <p className="font-medium text-muted-foreground">Prompt evidence</p>
                    {attempt.prompt_evidence.map((evidence) => (
                      <div key={evidence.evidence_id} className="rounded bg-muted/60 p-2">
                        <p>
                          <span className="font-medium">{evidence.label}</span> · {evidence.stage}
                        </p>
                        <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                          {evidence.excerpt}
                        </p>
                        <code className="mt-1 block break-all font-mono text-[11px] text-muted-foreground">
                          {evidence.content_sha256}
                        </code>
                      </div>
                    ))}
                  </div>
                ) : null}
                {attempt.error ? (
                  <pre className="mt-2 whitespace-pre-wrap rounded-md bg-muted p-2 text-xs">
                    {attempt.error}
                  </pre>
                ) : null}
              </div>
            ))}
          </section>
        ) : null}
      </CardContent>
    </Card>
  )
}
