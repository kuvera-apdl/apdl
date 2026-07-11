// Per-changeset detail (/codegen/:id). The list view is a scannable summary;
// this page is the diagnostic surface — the full task + spec the agent was
// given, the lifecycle stage it reached, PR/CI/diff facts, and (crucially) the
// UNTRUNCATED failure reason for an error run, which is the one
// thing an operator needs to know why an autonomous PR never opened.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronRight, ExternalLink, GitBranch } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import {
  abandonChangeset,
  getChangeset,
  getChangesetObservations,
  getRuntimeEvidenceObservations,
  retryChangeset,
  revertChangeset,
} from '@/api/codegen'
import { ApiError } from '@/api/http'
import { TERMINAL_CHANGESET_STATUSES } from '@/api/schemas/codegen'
import type { Changeset, ChangesetPrompt } from '@/api/types/codegen'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { queryKeys } from '@/core/queryClient'
import { cn } from '@/lib/utils'
import { serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'
import { ChangesetObservationHistory } from '@/features/codegen/ChangesetObservationHistory'
import { PublicationAuthorizationCard } from '@/features/codegen/PublicationAuthorizationCard'
import {
  RuntimeAcceptancePlanCard,
  RuntimeEvidenceHistory,
} from '@/features/codegen/RuntimeAcceptanceEvidence'
import {
  ChangesetStatusPill,
  ExternalCIStatusPill,
  GitHubPRStatusPill,
} from '@/features/codegen/ChangesetStatusPill'

const REFETCH_MS = 5000

// Happy-path lifecycle stages, in order. A failure status maps to the stage it
// died on (STAGE_OF); statuses that can fail anywhere map to -1 (no highlight).
const STAGE_LABELS = ['Queued', 'Clone', 'Edit', 'Push', 'PR', 'Merged'] as const
const STAGE_OF: Record<string, number> = {
  queued: 0,
  cloning: 1,
  editing: 2,
  pushing: 3,
  pr_open: 4,
  abandoned: 4,
  merged: 5,
  error: -1,
}
const FAILED_STATUSES = new Set(['error'])

function statNumber(diffStat: Record<string, unknown>, key: string): number | null {
  const value = diffStat[key]
  return typeof value === 'number' ? value : null
}

// The spec the agent receives often carries a trailing JSON metadata blob
// (dependencies / effort / components / considerations) appended after the
// human prose. Split them so each renders in its natural shape.
function splitSpec(spec: string): { prose: string; meta: Record<string, unknown> | null } {
  const idx = spec.lastIndexOf('\n\n{')
  if (idx === -1) return { prose: spec.trim(), meta: null }
  try {
    const parsed: unknown = JSON.parse(spec.slice(idx + 2))
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { prose: spec.slice(0, idx).trim(), meta: parsed as Record<string, unknown> }
    }
  } catch {
    // Trailing brace was not JSON — treat the whole thing as prose.
  }
  return { prose: spec.trim(), meta: null }
}

function metaList(meta: Record<string, unknown>, key: string): string[] {
  const value = meta[key]
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function LifecycleStepper({ status }: { status: string }) {
  const failed = FAILED_STATUSES.has(status)
  const current = STAGE_OF[status] ?? -1
  return (
    <ol className="flex flex-wrap items-center gap-1.5">
      {STAGE_LABELS.map((label, index) => {
        const isFailedStage = failed && index === current
        const state =
          current === -1
            ? 'pending'
            : isFailedStage
              ? 'failed'
              : index < current
                ? 'done'
                : index === current
                  ? 'active'
                  : 'pending'
        return (
          <li key={label} className="flex items-center gap-1.5">
            {index > 0 ? <span className="text-muted-foreground/40">→</span> : null}
            <span
              className={cn(
                'inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium',
                state === 'done' && 'border-emerald-300 text-emerald-700 dark:text-emerald-400',
                state === 'active' &&
                  'border-sky-400 bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300',
                state === 'failed' &&
                  'border-red-400 bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-300',
                state === 'pending' && 'text-muted-foreground',
              )}
            >
              {label}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

// Stage → pill styling for the prompt transcript. Unknown stages (a future
// codegen version) fall back to a neutral pill rather than breaking the page.
const PROMPT_STAGE_STYLES: Record<string, string> = {
  brief: 'border-sky-300 bg-sky-50 text-sky-800 dark:border-sky-800 dark:bg-sky-950/40 dark:text-sky-300',
  edit: 'border-violet-300 bg-violet-50 text-violet-800 dark:border-violet-800 dark:bg-violet-950/40 dark:text-violet-300',
  review:
    'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300',
}

function PromptText({ label, text }: { label: string; text: string }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
        {text}
      </pre>
    </div>
  )
}

function PromptEntry({ prompt }: { prompt: ChangesetPrompt }) {
  const chars = (prompt.system?.length ?? 0) + prompt.user.length
  return (
    <details className="group rounded-md border">
      <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 px-3 py-2 text-sm font-medium [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <span
          className={cn(
            'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
            PROMPT_STAGE_STYLES[prompt.stage] ?? 'bg-muted text-muted-foreground',
          )}
        >
          {prompt.stage}
        </span>
        {prompt.label}
        <span className="ml-auto text-xs font-normal tabular-nums text-muted-foreground">
          {chars.toLocaleString()} chars
        </span>
      </summary>
      <div className="space-y-3 border-t px-3 py-3">
        {prompt.notes ? <p className="text-xs text-muted-foreground">{prompt.notes}</p> : null}
        {prompt.system !== null ? <PromptText label="System prompt" text={prompt.system} /> : null}
        <PromptText label="User message" text={prompt.user} />
      </div>
    </details>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="text-sm">{children}</div>
    </div>
  )
}

function verificationLabel(value: string): string {
  return value
    .split('_')
    .map((word, index) => {
      if (word === 'github') return 'GitHub'
      if (word === 'ci' || word === 'api' || word === 'sdk' || word === 'ui') {
        return word.toUpperCase()
      }
      return index === 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word
    })
    .join(' ')
}

export function ChangesetDetailPage() {
  const { id = '' } = useParams()
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  const ws = active as Workspace

  const query = useQuery<Changeset>({
    queryKey: queryKeys.changeset(active?.id ?? 'none', id),
    enabled: active !== null && id !== '',
    refetchInterval: (q) =>
      q.state.data && TERMINAL_CHANGESET_STATUSES.has(q.state.data.status) ? false : REFETCH_MS,
    queryFn: ({ signal }) => getChangeset(serviceConnection(ws, 'codegen'), id, { signal }),
  })

  const observations = useQuery({
    queryKey: queryKeys.changesetObservations(active?.id ?? 'none', id),
    enabled: active !== null && id !== '' && query.data?.pr_number != null,
    refetchInterval: query.data?.status === 'merged' ? false : REFETCH_MS,
    queryFn: ({ signal }) =>
      getChangesetObservations(serviceConnection(ws, 'codegen'), id, { signal }),
  })

  const runtimeObservations = useQuery({
    queryKey: queryKeys.changesetRuntimeObservations(active?.id ?? 'none', id),
    enabled:
      active !== null &&
      id !== '' &&
      query.data?.pr_number != null &&
      query.data?.runtime_acceptance_plan != null,
    refetchInterval: query.data?.status === 'merged' ? false : REFETCH_MS,
    queryFn: ({ signal }) =>
      getRuntimeEvidenceObservations(serviceConnection(ws, 'codegen'), id, { signal }),
  })

  const invalidate = () => {
    if (active) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.changeset(active.id, id) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.changesets(active.id) })
    }
  }
  const onError = (fallback: string) => (error: Error) =>
    toast.error(error instanceof ApiError ? error.message : fallback)

  const abandon = useMutation({
    mutationFn: () => abandonChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Changeset abandoned')
      invalidate()
    },
    onError: onError('Abandon failed'),
  })
  const revert = useMutation({
    mutationFn: () => revertChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Revert PR requested')
      invalidate()
    },
    onError: onError('Revert failed'),
  })
  const retry = useMutation({
    mutationFn: () => retryChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Retry started')
      invalidate()
    },
    onError: onError('Retry failed'),
  })
  const busy = abandon.isPending || revert.isPending || retry.isPending

  if (query.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }
  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404
    return notFound ? (
      <EmptyState title="Changeset not found" description="The codegen service has no record of this changeset id." />
    ) : (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} />
    )
  }

  const cs: Changeset = query.data
  const hasPullRequest = cs.pr_number !== null || cs.pr_url !== null
  const files = statNumber(cs.diff_stat, 'files')
  const additions = statNumber(cs.diff_stat, 'additions')
  const deletions = statNumber(cs.diff_stat, 'deletions')
  const { prose, meta } = splitSpec(cs.task.spec)

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/codegen', label: 'Code changes' }}
        title={
          <span className="flex flex-wrap items-center gap-2">
            {cs.task.title}
            <ChangesetStatusPill status={cs.status} />
          </span>
        }
        description={
          <>
            <code className="font-mono text-xs">{cs.changeset_id}</code> · created{' '}
            <RelativeTime value={cs.created_at} /> · updated <RelativeTime value={cs.updated_at} />
          </>
        }
        actions={
          cs.status === 'merged' ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => revert.mutate()}>
              Revert
            </Button>
          ) : hasPullRequest ? (
            cs.pr_url ? (
              <Button size="sm" asChild>
                <a href={cs.pr_url} target="_blank" rel="noreferrer">Open PR on GitHub</a>
              </Button>
            ) : null
          ) : cs.status === 'error' ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => retry.mutate()}>
              Retry
            </Button>
          ) : cs.status === 'queued' ? (
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => abandon.mutate()}>
              Abandon
            </Button>
          ) : null
        }
      />

      {cs.error ? (
        <Card className="border-red-400 dark:border-red-800">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-red-700 dark:text-red-400">
              <AlertTriangle className="h-5 w-5" />
              Failure reason
            </CardTitle>
            <CardDescription>
              Why generation ended in <code className="font-mono">{cs.status}</code> before APDL could complete its lifecycle step.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {cs.error}
            </pre>
          </CardContent>
        </Card>
      ) : null}

      {cs.ci_failure_summary ? (
        <Card className="border-amber-400 dark:border-amber-800">
          <CardHeader>
            <CardTitle>GitHub CI failure</CardTitle>
            <CardDescription>
              Evidence used by APDL's bounded same-branch repair loop.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {cs.ci_failure_summary}
            </pre>
          </CardContent>
        </Card>
      ) : null}

      {cs.external_ci_status === 'unverified_external_ci' ? (
        <Card className="border-amber-400 dark:border-amber-800">
          <CardHeader>
            <CardTitle>No external CI configured</CardTitle>
            <CardDescription>
              GitHub reported no CI signals for the exact PR head. This changeset is unverified;
              absence of CI is never represented as passed.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>APDL lifecycle</CardTitle>
          <CardDescription>
            APDL generation and PR-publication stage only. GitHub PR and CI state are separate.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <LifecycleStepper status={cs.status} />
        </CardContent>
      </Card>

      {cs.publication_authorization ? (
        <PublicationAuthorizationCard authorization={cs.publication_authorization} />
      ) : null}

      {cs.requirement_ledger ? (
        <Card>
          <CardHeader>
            <CardTitle>Requirement ledger</CardTitle>
            <CardDescription>
              Stable implementation mappings and the GitHub CI evidence expected for each criterion.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {cs.requirement_ledger.requirements.map((requirement) => (
              <div key={requirement.requirement_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <code className="font-mono text-xs">{requirement.requirement_id}</code>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                    {requirement.implementation_status}
                  </span>
                  <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                    {requirement.risk} risk
                  </span>
                </div>
                <p className="mt-2">{requirement.observable_behavior}</p>
                {requirement.implementation_evidence.length > 0 ? (
                  <p className="mt-2 text-xs text-muted-foreground">
                    Code: {requirement.implementation_evidence.map((item) => item.path).join(', ')}
                  </p>
                ) : null}
                {requirement.expected_ci_evidence.length > 0 ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Expected GitHub evidence:{' '}
                    {requirement.expected_ci_evidence.map((item) => item.evidence_id).join(', ')}
                  </p>
                ) : requirement.decision_reason ? (
                  <p className="mt-1 text-xs text-muted-foreground">
                    Decision: {requirement.decision_reason}
                  </p>
                ) : null}
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {cs.verification_plan ? (
        <Card>
          <CardHeader>
            <CardTitle>Verification plan</CardTitle>
            <CardDescription>
              Required regression evidence planned before the diff is handed to GitHub CI.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Fact label="Disposition">
                {verificationLabel(cs.verification_plan.disposition)}
              </Fact>
              <Fact label="Authority">GitHub CI</Fact>
              <Fact label="Risk">{verificationLabel(cs.verification_plan.risk)}</Fact>
              <Fact label="Test runner">
                {cs.verification_plan.test_runner_configured ? 'Configured' : 'Not configured'}
              </Fact>
            </div>
            <p className="text-sm text-muted-foreground">
              {cs.verification_plan.disposition_reason}
            </p>
            {cs.verification_plan.test_commands.length > 0 ? (
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Repository test commands
                </p>
                <ul className="mt-1 space-y-1 text-sm">
                  {cs.verification_plan.test_commands.map((testCommand) => (
                    <li key={`${testCommand.cwd}:${testCommand.command}`}>
                      <code className="font-mono text-xs">{testCommand.command}</code>
                      <span className="text-muted-foreground"> in {testCommand.cwd}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {cs.verification_plan.github_workflow_paths.length > 0 ? (
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  GitHub workflows
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {cs.verification_plan.github_workflow_paths.map((path) => (
                    <li key={path}>
                      <code className="font-mono text-xs">{path}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {cs.verification_plan.items.length > 0 ? (
              <div className="space-y-2">
                {cs.verification_plan.items.map((item) => (
                  <div key={item.plan_item_id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs">{item.plan_item_id}</code>
                      <code className="font-mono text-xs text-muted-foreground">
                        {item.requirement_id}
                      </code>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                        {verificationLabel(item.surface)}
                      </span>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                        {item.requirement_risk} risk
                      </span>
                    </div>
                    <p className="mt-2">{item.expected_assertion}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Expected GitHub evidence: {item.expected_ci_evidence_ids.join(', ')}
                    </p>
                  </div>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {cs.verification_coverage ? (
        <Card>
          <CardHeader>
            <CardTitle>Pre-CI coverage</CardTitle>
            <CardDescription>
              APDL records whether the diff contains planned coverage paths. GitHub remains
              authoritative for executing CI and reporting its result.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Fact label="Disposition">
                {verificationLabel(cs.verification_coverage.disposition)}
              </Fact>
              <Fact label="Changed tests">
                {cs.verification_coverage.changed_test_paths.length}
              </Fact>
              <Fact label="Workflow policy">
                {verificationLabel(cs.verification_coverage.workflow_gate_policy)}
              </Fact>
            </div>
            <p className="text-sm text-muted-foreground">
              {cs.verification_coverage.disposition_reason}
            </p>
            {cs.verification_coverage.changed_test_paths.length > 0 ? (
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Changed test paths
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {cs.verification_coverage.changed_test_paths.map((path) => (
                    <li key={path}>
                      <code className="font-mono text-xs">{path}</code>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {cs.verification_coverage.items.length > 0 ? (
              <div className="space-y-2">
                {cs.verification_coverage.items.map((item) => (
                  <div
                    key={item.plan_item_id}
                    className="flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm"
                  >
                    <code className="font-mono text-xs">{item.plan_item_id}</code>
                    <span>{verificationLabel(item.status)}</span>
                    {item.coverage_paths.length > 0 ? (
                      <span className="text-xs text-muted-foreground">
                        {item.coverage_paths.join(', ')}
                      </span>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {cs.runtime_acceptance_plan ? (
        <RuntimeAcceptancePlanCard plan={cs.runtime_acceptance_plan} />
      ) : null}

      {cs.review_verdict ? (
        <Card>
          <CardHeader>
            <CardTitle>Semantic review</CardTitle>
            <CardDescription>
              APDL&apos;s evidence-backed pre-push review of the exact generated diff. This is not a
              GitHub CI result; GitHub remains authoritative for checks, review policy, and merge.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              <Fact label="Decision">
                {verificationLabel(cs.review_verdict.overall_decision)}
              </Fact>
              <Fact label="Model response">
                {verificationLabel(cs.review_verdict.model_response_status)}
              </Fact>
              <Fact label="Deterministic findings">
                {cs.review_verdict.deterministic_findings.length}
              </Fact>
            </div>
            <Fact label="Reviewed diff SHA-256">
              <code className="font-mono text-xs break-all">
                {cs.review_verdict.reviewed_diff_sha256}
              </code>
            </Fact>
            <p className="text-xs text-muted-foreground">
              Deterministic errors override any model approval.
            </p>
            {cs.review_verdict.requirement_decisions.length > 0 ? (
              <div className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Requirement decisions
                </p>
                {cs.review_verdict.requirement_decisions.map((decision) => (
                  <div key={decision.requirement_id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs">{decision.requirement_id}</code>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                        {verificationLabel(decision.decision)}
                      </span>
                    </div>
                    <p className="mt-2">{decision.rationale}</p>
                    {decision.evidence_ids.length > 0 ? (
                      <p className="mt-1 text-xs text-muted-foreground">
                        Evidence: {decision.evidence_ids.join(', ')}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}
            {cs.review_verdict.deterministic_findings.length > 0 ? (
              <div className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Deterministic findings
                </p>
                {cs.review_verdict.deterministic_findings.map((finding) => (
                  <div key={finding.finding_id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs">{finding.finding_id}</code>
                      <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                        {verificationLabel(finding.severity)}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {verificationLabel(finding.code)}
                      </span>
                    </div>
                    <p className="mt-2">{finding.message}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Action: {finding.actionable_instruction}
                    </p>
                  </div>
                ))}
              </div>
            ) : null}
            {cs.review_verdict.uncertainties.length > 0 ? (
              <div className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Uncertainties
                </p>
                {cs.review_verdict.uncertainties.map((uncertainty) => (
                  <div key={uncertainty.uncertainty_id} className="rounded-md border p-3 text-sm">
                    <div className="flex flex-wrap items-center gap-2">
                      <code className="font-mono text-xs">{uncertainty.uncertainty_id}</code>
                      <span className="text-xs text-muted-foreground">
                        {verificationLabel(uncertainty.code)}
                      </span>
                    </div>
                    <p className="mt-2">{uncertainty.message}</p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Resolution: {uncertainty.resolution_instruction}
                    </p>
                  </div>
                ))}
              </div>
            ) : null}
            {cs.review_verdict.actionable_instructions.length > 0 ? (
              <div>
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Required actions
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {cs.review_verdict.actionable_instructions.map((instruction, index) => (
                    <li key={`${index}:${instruction}`}>{instruction}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {cs.dependency_slice ? (
        <Card>
          <CardHeader>
            <CardTitle>Repository evidence</CardTitle>
            <CardDescription>
              Content-addressed files, callers, routes, and tests connected to the generated diff.
            </CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Fact label="Changed files">{cs.dependency_slice.changed_files.length}</Fact>
            <Fact label="Local dependencies">
              {cs.dependency_slice.imported_local_symbols.length}
            </Fact>
            <Fact label="Callers">{cs.dependency_slice.callers.length}</Fact>
            <Fact label="Affected tests">{cs.dependency_slice.affected_tests.length}</Fact>
            {cs.dependency_slice.unresolved_references.length > 0 ? (
              <div className="sm:col-span-2 lg:col-span-4">
                <p className="text-xs font-medium uppercase tracking-wide text-amber-700 dark:text-amber-400">
                  Unresolved references
                </p>
                <ul className="mt-1 list-disc space-y-1 pl-5 text-sm">
                  {cs.dependency_slice.unresolved_references.map((reference) => (
                    <li key={reference}>{reference}</li>
                  ))}
                </ul>
              </div>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
          <Fact label="Base branch">
            <code className="font-mono">{cs.base_branch ?? '—'}</code>
          </Fact>
          <Fact label="Work branch">
            {cs.branch ? (
              <span className="inline-flex items-center gap-1">
                <GitBranch className="h-3.5 w-3.5 text-muted-foreground" />
                <code className="font-mono break-all">{cs.branch}</code>
              </span>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Pull request">
            {cs.pr_url ? (
              <a
                href={cs.pr_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 underline"
              >
                #{cs.pr_number}
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="GitHub PR status">
            {cs.github_pr_status ? (
              <GitHubPRStatusPill status={cs.github_pr_status} />
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="External CI status">
            {cs.external_ci_status ? (
              <ExternalCIStatusPill status={cs.external_ci_status} />
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Exact PR head">
            {cs.head_sha ? (
              <code className="font-mono text-xs break-all" title={cs.head_sha}>
                {cs.head_sha}
              </code>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="CI repair">
            {cs.ci_remediation_status} · {cs.ci_retry_count} attempt
            {cs.ci_retry_count === 1 ? '' : 's'}
          </Fact>
          <Fact label="Awaiting external CI since">
            {cs.external_ci_awaiting_since ? (
              <RelativeTime value={cs.external_ci_awaiting_since} />
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Merge commit">
            {cs.merge_sha ? <code className="font-mono">{cs.merge_sha.slice(0, 12)}</code> : '—'}
          </Fact>
          <Fact label="Diff">
            {files !== null ? (
              <span className="tabular-nums">
                {files} file{files === 1 ? '' : 's'}
                {additions !== null ? (
                  <span className="text-emerald-600 dark:text-emerald-400"> +{additions}</span>
                ) : null}
                {deletions !== null ? (
                  <span className="text-red-600 dark:text-red-400"> −{deletions}</span>
                ) : null}
              </span>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Agent run">
            {cs.run_id ? (
              <Link to={`/agents/runs/${cs.run_id}`} className="font-mono text-xs underline">
                {cs.run_id.slice(0, 8)}…
              </Link>
            ) : (
              '—'
            )}
          </Fact>
        </CardContent>
      </Card>

      {cs.pr_number !== null ? (
        observations.isPending ? (
          <Skeleton className="h-48 w-full" />
        ) : observations.isError ? (
          <ErrorState error={observations.error} onRetry={() => void observations.refetch()} />
        ) : (
          <ChangesetObservationHistory history={observations.data} />
        )
      ) : null}

      {cs.runtime_acceptance_plan && cs.pr_number !== null ? (
        runtimeObservations.isPending ? (
          <Skeleton className="h-48 w-full" />
        ) : runtimeObservations.isError ? (
          <ErrorState
            error={runtimeObservations.error}
            onRetry={() => void runtimeObservations.refetch()}
          />
        ) : (
          <RuntimeEvidenceHistory
            observations={runtimeObservations.data}
            currentAssessment={cs.runtime_evidence_assessment}
            externalCIStatus={cs.external_ci_status}
          />
        )
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Task</CardTitle>
          <CardDescription>The specification handed to the editing agent.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{prose}</p>

          {meta ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {typeof meta.estimated_effort === 'string' ? (
                <Fact label="Estimated effort">
                  <span className="inline-flex items-center rounded-full border bg-muted px-2 py-0.5 text-xs font-medium">
                    {meta.estimated_effort}
                  </span>
                </Fact>
              ) : null}
              {(['dependencies', 'components_affected', 'technical_considerations'] as const).map((key) => {
                const items = metaList(meta, key)
                if (items.length === 0) return null
                return (
                  <Fact key={key} label={key.replace(/_/g, ' ')}>
                    <ul className="list-disc space-y-1 pl-4 text-sm text-muted-foreground">
                      {items.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </Fact>
                )
              })}
            </div>
          ) : null}

          {cs.task.constraints.length > 0 ? (
            <Fact label="Constraints">
              <ul className="list-disc space-y-1 pl-4 text-sm text-muted-foreground">
                {cs.task.constraints.map((constraint, i) => (
                  <li key={i}>{constraint}</li>
                ))}
              </ul>
            </Fact>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Prompts</CardTitle>
          <CardDescription>
            The complete system and user prompts this run sent, in order: the brief compilation
            (spec → repo-grounded work order), each instruction handed to the coding agent, and
            each pre-push diff review.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          {cs.prompts.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No prompts recorded for this run yet. They appear once the editing stage runs; runs
              from before prompt recording have none.
            </p>
          ) : (
            cs.prompts.map((prompt, i) => <PromptEntry key={i} prompt={prompt} />)
          )}
        </CardContent>
      </Card>
    </div>
  )
}
