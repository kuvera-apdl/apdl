import {
  Bot,
  Check,
  CircleAlert,
  Loader2,
  PauseCircle,
  Settings2,
  ShieldCheck,
} from 'lucide-react'
import {
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from 'react'
import { toast } from 'sonner'

import {
  deactivateAgentsSetup,
  putAgentsSetup,
} from '@/api/agentsSetup'
import { ApiError } from '@/api/http'
import type {
  AgentsModelSelection,
  AgentsSetup,
  AgentsSetupAssignment,
  LlmProvider,
  LlmProviderModel,
} from '@/api/types/agents-setup'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/core/auth'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { LlmConnectionsManager } from '@/features/agents/setup/LlmConnectionsManager'
import {
  useAgentsConnections,
  useAgentsModelInventories,
  useAgentsSetup,
  useInvalidateAgentsSetup,
} from '@/features/agents/setup/hooks'

type WizardStep = 'connections' | 'models' | 'review'

const PROVIDERS: readonly LlmProvider[] = [
  'openai',
  'anthropic',
  'google',
  'xai',
]

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  xai: 'xAI',
}

interface SelectableModel {
  model: LlmProviderModel
  connectionVersion: number
  inventoryVersion: number
}

const BLOCKER_LABELS: Record<AgentsSetup['blockers'][number], string> = {
  project_inactive: 'Project activation is incomplete',
  fast_model_required: 'Fast model is not assigned',
  reasoning_model_required: 'Reasoning model is not assigned',
  connection_inactive: 'An assigned provider connection is inactive',
  connection_stale: 'An assigned provider connection uses an older catalog',
  inventory_stale: 'An assigned model inventory has changed',
  model_unavailable: 'An assigned model is no longer available',
  model_ineligible: 'An assigned model is no longer eligible for its tier',
  catalog_stale: 'An assigned model uses an older reviewed catalog',
  credential_unavailable: 'An assigned provider credential is unavailable',
  budget_invalid: 'The project cost policy is invalid',
}

function modelKey(provider: string, model: string): string {
  return `${provider}\u001f${model}`
}

function assignmentKey(
  assignment: AgentsSetupAssignment | undefined,
): string {
  return assignment
    ? modelKey(assignment.provider, assignment.model)
    : ''
}

function selection(model: SelectableModel): AgentsModelSelection {
  return {
    provider: model.model.provider,
    model: model.model.model_id,
    connection_version: model.connectionVersion,
    inventory_version: model.inventoryVersion,
  }
}

function dollars(micros: number): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(micros / 1_000_000)
}

function setupError(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return 'Setup changed in another session. Model inventories were refreshed; review the selections again.'
  }
  if (error instanceof ApiError && error.status === 422) {
    return error.message
  }
  if (error instanceof ApiError && error.status === 403) {
    return 'Your live project authority no longer permits this setup change.'
  }
  if (error instanceof ApiError) return error.message
  return 'Agents setup failed. Try again shortly.'
}

function StepIndicator({ current }: { current: WizardStep }) {
  const steps: { id: WizardStep; label: string }[] = [
    { id: 'connections', label: 'Connections' },
    { id: 'models', label: 'Models' },
    { id: 'review', label: 'Review' },
  ]
  const currentIndex = steps.findIndex((step) => step.id === current)
  return (
    <ol className="grid grid-cols-3 gap-2" aria-label="Agents setup progress">
      {steps.map((step, index) => (
        <li
          key={step.id}
          className={`rounded-md border px-3 py-2 text-xs ${
            index === currentIndex
              ? 'border-foreground bg-muted font-semibold'
              : 'text-muted-foreground'
          }`}
          aria-current={index === currentIndex ? 'step' : undefined}
        >
          <span className="mr-1.5">{index + 1}.</span>
          {step.label}
        </li>
      ))}
    </ol>
  )
}

function ModelDetails({
  item,
  tier,
}: {
  item: SelectableModel | null
  tier: 'fast' | 'reasoning'
}) {
  if (!item) return null
  return (
    <div className="rounded-md border bg-muted/20 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{item.model.display_name}</span>
        <Badge variant="outline">{item.model.data_residency}</Badge>
        <Badge variant="outline">{item.model.catalog_version}</Badge>
      </div>
      <p className="mt-2 text-muted-foreground">
        {tier === 'fast' ? 'Input' : 'Reasoning input'}{' '}
        {dollars(item.model.input_cost_per_million_tokens_usd_micros)} / 1M
        tokens · output{' '}
        {dollars(item.model.output_cost_per_million_tokens_usd_micros)} / 1M
        tokens
      </p>
      <p className="mt-1 font-mono text-muted-foreground">
        {item.model.endpoint_host}
      </p>
    </div>
  )
}

function AgentsSetupWizard({
  setup,
  open,
  onClose,
  onSetupChanged,
}: {
  setup: AgentsSetup
  open: boolean
  onClose: () => void
  onSetupChanged: (committedSetup?: AgentsSetup) => Promise<void>
}) {
  const { active, projectId } = useWorkspace()
  const { identity, refreshIdentity } = useAuth()
  const connectionsQuery = useAgentsConnections()
  const activeConnections = useMemo(
    () =>
      (connectionsQuery.data?.connections ?? []).filter(
        (connection) =>
          connection.state === 'active' &&
          setup.connections.some(
            (setupConnection) =>
              setupConnection.provider === connection.provider &&
              setupConnection.connection_version === connection.version &&
              setupConnection.inventory_version ===
                connection.inventory_version &&
              setupConnection.catalog_version ===
                connection.catalog_version &&
              setupConnection.state === 'active' &&
              setupConnection.current,
          ),
      ),
    [connectionsQuery.data, setup.connections],
  )
  const inventoryQueries = useAgentsModelInventories(activeConnections)
  const [step, setStep] = useState<WizardStep>('connections')
  const [fastKey, setFastKey] = useState('')
  const [reasoningKey, setReasoningKey] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    const fast = setup.assignments.find(
      (assignment) => assignment.tier === 'fast',
    )
    const reasoning = setup.assignments.find(
      (assignment) => assignment.tier === 'reasoning',
    )
    setFastKey(assignmentKey(fast))
    setReasoningKey(assignmentKey(reasoning))
    setStep(
      setup.state === 'active' && setup.assignments.length === 2
        ? 'models'
        : 'connections',
    )
    setError(null)
  }, [open, setup.project_id, setup.version])

  const selectableModels = useMemo<SelectableModel[]>(
    () =>
      inventoryQueries.flatMap((query, index) => {
        const inventory = query.data
        const connection = activeConnections[index]
        if (!inventory || !connection) return []
        return inventory.models.map((model) => ({
          model,
          connectionVersion: inventory.connection_version,
          inventoryVersion: inventory.inventory_version,
        }))
      }),
    [activeConnections, inventoryQueries],
  )
  const fastModels = selectableModels.filter((item) =>
    item.model.supported_tiers.includes('fast'),
  )
  const reasoningModels = selectableModels.filter((item) =>
    item.model.supported_tiers.includes('reasoning'),
  )
  const fastModel =
    fastModels.find(
      (item) => modelKey(item.model.provider, item.model.model_id) === fastKey,
    ) ?? null
  const reasoningModel =
    reasoningModels.find(
      (item) =>
        modelKey(item.model.provider, item.model.model_id) === reasoningKey,
    ) ?? null
  const inventoriesPending = inventoryQueries.some((query) => query.isPending)
  const inventoriesFailed = inventoryQueries.some((query) => query.isError)
  const residencyMismatch =
    fastModel !== null &&
    reasoningModel !== null &&
    fastModel.model.data_residency !== reasoningModel.model.data_residency
  const firstActivation = setup.state === 'inactive'

  const refreshAfterConnectionChange = async () => {
    await Promise.all([
      connectionsQuery.refetch(),
      onSetupChanged(),
    ])
  }

  const submitSetup = async () => {
    if (
      !active ||
      !projectId ||
      !fastModel ||
      !reasoningModel ||
      residencyMismatch
    ) {
      return
    }
    setPending(true)
    setError(null)
    try {
      const committedSetup = await putAgentsSetup(
        serviceConnection(active, 'agents'),
        projectId,
        selection(fastModel),
        selection(reasoningModel),
        setup.version,
      )
      const synchronization = await Promise.allSettled([
        onSetupChanged(committedSetup),
        ...(firstActivation ? [refreshIdentity()] : []),
      ])
      toast.success(
        firstActivation
          ? 'Agentic runs activated'
          : 'Agent model assignments updated',
      )
      if (synchronization.some((result) => result.status === 'rejected')) {
        toast.warning(
          'Setup was saved, but part of this view could not refresh. Reload to reconcile current access.',
        )
      }
      onClose()
    } catch (caught) {
      setError(setupError(caught))
      if (caught instanceof ApiError && caught.status === 409) {
        await refreshAfterConnectionChange()
        setStep('models')
      }
    } finally {
      setPending(false)
    }
  }

  const modelsReady =
    activeConnections.length > 0 &&
    !inventoriesPending &&
    !inventoriesFailed &&
    fastModels.length > 0 &&
    reasoningModels.length > 0

  return (
    <Dialog open={open} onOpenChange={(next) => !next && !pending && onClose()}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>
            {firstActivation ? 'Set up Agentic runs' : 'Manage Agentic runs'}
          </DialogTitle>
          <DialogDescription>
            Connect provider credentials, assign one current model to each
            execution tier, then review the exact runtime policy.
          </DialogDescription>
        </DialogHeader>

        <StepIndicator current={step} />

        {step === 'connections' ? (
          <div className="space-y-4">
            <div className="rounded-md border bg-muted/20 p-3 text-sm">
              <p>
                Project{' '}
                <code className="font-mono font-medium">{projectId}</code>
              </p>
              <p className="mt-1 text-xs text-muted-foreground">
                {setup.caller_capabilities.management_authority === 'owner'
                  ? `Current owner: ${identity?.email ?? 'current signed-in owner'}`
                  : setup.caller_capabilities.management_authority ===
                      'delegated'
                    ? `Delegated manager: ${identity?.email ?? 'current signed-in manager'}`
                    : 'Read-only project setup'}
              </p>
            </div>
            <LlmConnectionsManager
              canManage={setup.caller_capabilities.can_manage}
              onChanged={() => void refreshAfterConnectionChange()}
              assignedProviders={setup.assignments.map(
                (assignment) => assignment.provider,
              )}
            />
            {connectionsQuery.data && activeConnections.length === 0 ? (
              <p className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                Add or refresh a current active connection before continuing.
              </p>
            ) : null}
          </div>
        ) : null}

        {step === 'models' ? (
          <div className="space-y-5">
            {inventoriesPending ? (
              <p
                className="flex items-center gap-2 rounded-md border p-4 text-sm text-muted-foreground"
                role="status"
              >
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading current model inventories…
              </p>
            ) : null}
            {inventoriesFailed ? (
              <p className="rounded-md border border-destructive/40 p-3 text-sm text-destructive">
                One or more current model inventories could not be loaded.
                Return to connections and refresh them.
              </p>
            ) : null}
            <div className="space-y-2">
              <Label htmlFor="agents-fast-model">Fast model</Label>
              <p className="text-xs text-muted-foreground">
                Used for lower-latency classification and orchestration work.
              </p>
              <Select
                id="agents-fast-model"
                value={fastKey}
                disabled={!modelsReady || pending}
                onChange={(event) => {
                  setFastKey(event.target.value)
                  setError(null)
                }}
              >
                <option value="">Select a fast model</option>
                {PROVIDERS.map((provider) => {
                  const items = fastModels.filter(
                    (item) => item.model.provider === provider,
                  )
                  return items.length > 0 ? (
                    <optgroup
                      key={provider}
                      label={PROVIDER_LABELS[provider]}
                    >
                      {items.map((item) => (
                        <option
                          key={modelKey(
                            item.model.provider,
                            item.model.model_id,
                          )}
                          value={modelKey(
                            item.model.provider,
                            item.model.model_id,
                          )}
                        >
                          {item.model.display_name} ·{' '}
                          {item.model.data_residency}
                        </option>
                      ))}
                    </optgroup>
                  ) : null
                })}
              </Select>
              <ModelDetails item={fastModel} tier="fast" />
            </div>
            <div className="space-y-2">
              <Label htmlFor="agents-reasoning-model">Reasoning model</Label>
              <p className="text-xs text-muted-foreground">
                Used for analysis, experiment design, and proposals.
              </p>
              <Select
                id="agents-reasoning-model"
                value={reasoningKey}
                disabled={!modelsReady || pending}
                onChange={(event) => {
                  setReasoningKey(event.target.value)
                  setError(null)
                }}
              >
                <option value="">Select a reasoning model</option>
                {PROVIDERS.map((provider) => {
                  const items = reasoningModels.filter(
                    (item) => item.model.provider === provider,
                  )
                  return items.length > 0 ? (
                    <optgroup
                      key={provider}
                      label={PROVIDER_LABELS[provider]}
                    >
                      {items.map((item) => (
                        <option
                          key={modelKey(
                            item.model.provider,
                            item.model.model_id,
                          )}
                          value={modelKey(
                            item.model.provider,
                            item.model.model_id,
                          )}
                        >
                          {item.model.display_name} ·{' '}
                          {item.model.data_residency}
                        </option>
                      ))}
                    </optgroup>
                  ) : null
                })}
              </Select>
              <ModelDetails item={reasoningModel} tier="reasoning" />
            </div>
            {residencyMismatch ? (
              <p
                className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
                role="alert"
              >
                Fast and reasoning models must use the same reviewed data
                residency.
              </p>
            ) : null}
          </div>
        ) : null}

        {step === 'review' && fastModel && reasoningModel ? (
          <div className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                ['Fast', fastModel],
                ['Reasoning', reasoningModel],
              ].map(([label, item]) => {
                const selected = item as SelectableModel
                return (
                  <div key={label as string} className="rounded-md border p-3">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                      {label as string}
                    </p>
                    <p className="mt-1 font-medium">
                      {selected.model.display_name}
                    </p>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {selected.model.provider}/{selected.model.model_id}
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Connection v{selected.connectionVersion} · inventory v
                      {selected.inventoryVersion} ·{' '}
                      {selected.model.catalog_version}
                    </p>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {selected.model.endpoint_host}
                    </p>
                    <p className="mt-2 text-xs text-muted-foreground">
                      Input{' '}
                      {dollars(
                        selected.model
                          .input_cost_per_million_tokens_usd_micros,
                      )}{' '}
                      · output{' '}
                      {dollars(
                        selected.model
                          .output_cost_per_million_tokens_usd_micros,
                      )}{' '}
                      per 1M tokens
                    </p>
                    <p className="mt-1 text-xs text-muted-foreground">
                      Data classes:{' '}
                      {selected.model.allowed_data_classifications.join(', ')}
                    </p>
                  </div>
                )
              })}
            </div>
            <div className="rounded-md border p-3 text-sm">
              <p className="font-medium">Runtime guardrails</p>
              <dl className="mt-2 grid gap-2 text-xs sm:grid-cols-2">
                <div>
                  <dt className="text-muted-foreground">Data residency</dt>
                  <dd>{fastModel.model.data_residency}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Cross-vendor retry</dt>
                  <dd>Disabled</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Daily project limit</dt>
                  <dd>
                    {dollars(
                      setup.policy.project_daily_cost_limit_usd_micros,
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Per-run limit</dt>
                  <dd>
                    {dollars(setup.policy.run_cost_limit_usd_micros)}
                  </dd>
                </div>
              </dl>
            </div>
            <p className="flex gap-2 rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
              <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
              Activation permits governed L1/L2 analysis only. It does not
              grant agents:approve, L3/L4 autonomous mutation, Codegen,
              repository access, or any external effect. Effectful execution
              remains separately controlled and is currently{' '}
              {setup.effectful_execution.authorized
                ? `authorized (${setup.effectful_execution.authorization_source})`
                : 'not authorized'}
              .
            </p>
          </div>
        ) : null}

        {error ? (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        ) : null}

        <DialogFooter className="sm:justify-between">
          <Button
            type="button"
            variant="outline"
            disabled={pending}
            onClick={() => {
              if (step === 'connections') onClose()
              else setStep(step === 'review' ? 'models' : 'connections')
            }}
          >
            {step === 'connections' ? 'Set up later' : 'Back'}
          </Button>
          {step === 'connections' ? (
            <Button
              type="button"
              disabled={
                connectionsQuery.isPending ||
                activeConnections.length === 0 ||
                !setup.caller_capabilities.can_manage
              }
              onClick={() => setStep('models')}
            >
              Continue to models
            </Button>
          ) : null}
          {step === 'models' ? (
            <Button
              type="button"
              disabled={
                !fastModel ||
                !reasoningModel ||
                residencyMismatch ||
                pending
              }
              onClick={() => setStep('review')}
            >
              Review setup
            </Button>
          ) : null}
          {step === 'review' ? (
            <Button
              type="button"
              disabled={
                pending ||
                !fastModel ||
                !reasoningModel ||
                (firstActivation
                  ? !setup.caller_capabilities.can_activate
                  : !setup.caller_capabilities.can_manage)
              }
              onClick={() => void submitSetup()}
            >
              {pending ? <Loader2 className="animate-spin" /> : <Check />}
              {firstActivation ? 'Activate Agentic runs' : 'Save assignments'}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function AgenticRunsCard({
  autoOpen = false,
  onAutoOpenHandled,
}: {
  autoOpen?: boolean
  onAutoOpenHandled?: () => void
}) {
  const { active, projectId } = useWorkspace()
  const setupQuery = useAgentsSetup()
  const invalidateSetup = useInvalidateAgentsSetup()
  const [wizardOpen, setWizardOpen] = useState(false)
  const [deactivateOpen, setDeactivateOpen] = useState(false)
  const [deactivationReason, setDeactivationReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [pending, setPending] = useState(false)
  const [deactivationError, setDeactivationError] = useState<string | null>(
    null,
  )

  useEffect(() => {
    if (!autoOpen || !setupQuery.data) return
    setWizardOpen(true)
    onAutoOpenHandled?.()
  }, [autoOpen, onAutoOpenHandled, setupQuery.data])

  useEffect(() => {
    setWizardOpen(false)
    setDeactivateOpen(false)
    setDeactivationReason('')
    setConfirmed(false)
    setDeactivationError(null)
  }, [active?.id])

  if (!active || !projectId) return null

  const setup = setupQuery.data
  const deactivate = async (event: FormEvent) => {
    event.preventDefault()
    if (
      !setup ||
      !confirmed ||
      !deactivationReason.trim() ||
      !setup.caller_capabilities.can_deactivate
    ) {
      return
    }
    setPending(true)
    setDeactivationError(null)
    try {
      const committedSetup = await deactivateAgentsSetup(
        serviceConnection(active, 'agents'),
        projectId,
        setup.version,
        deactivationReason.trim(),
      )
      const synchronization = await Promise.allSettled([
        invalidateSetup(committedSetup),
      ])
      toast.success('Agentic runs deactivated')
      if (synchronization.some((result) => result.status === 'rejected')) {
        toast.warning(
          'Deactivation succeeded, but this view could not fully refresh. Reload to reconcile current access.',
        )
      }
      setDeactivateOpen(false)
      setDeactivationReason('')
      setConfirmed(false)
    } catch (error) {
      setDeactivationError(setupError(error))
      if (error instanceof ApiError && error.status === 409) {
        await invalidateSetup()
      }
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <Bot className="h-5 w-5" />
                Agentic runs
              </CardTitle>
              <CardDescription>
                Project-scoped LLM connections, model assignments, and
                autonomous analysis activation.
              </CardDescription>
            </div>
            {setup ? (
              <Badge
                variant={
                  setup.analysis_ready
                    ? 'default'
                    : setup.state === 'active'
                      ? 'destructive'
                      : 'secondary'
                }
              >
                {setup.analysis_ready
                  ? 'Active'
                  : setup.state === 'active'
                    ? 'Blocked'
                    : 'Inactive'}
              </Badge>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {setupQuery.isPending ? (
            <p
              className="flex items-center gap-2 py-4 text-sm text-muted-foreground"
              role="status"
            >
              <Loader2 className="h-4 w-4 animate-spin" />
              Loading Agentic runs setup…
            </p>
          ) : null}
          {setupQuery.isError ? (
            <ErrorState
              error={setupQuery.error}
              onRetry={() => void setupQuery.refetch()}
            />
          ) : null}
          {setup ? (
            <>
              <div className="grid gap-3 sm:grid-cols-2">
                {(['fast', 'reasoning'] as const).map((tier) => {
                  const assignment = setup.assignments.find(
                    (item) => item.tier === tier,
                  )
                  return (
                    <div key={tier} className="rounded-md border p-3">
                      <p className="text-xs font-medium capitalize text-muted-foreground">
                        {tier} model
                      </p>
                      {assignment ? (
                        <>
                          <p className="mt-1 text-sm font-medium">
                            {assignment.display_name}
                          </p>
                          <p className="mt-1 font-mono text-xs text-muted-foreground">
                            {assignment.provider}/{assignment.model}
                          </p>
                          {!assignment.current ? (
                            <Badge variant="destructive" className="mt-2">
                              Needs review
                            </Badge>
                          ) : null}
                        </>
                      ) : (
                        <p className="mt-1 text-sm text-muted-foreground">
                          Not assigned
                        </p>
                      )}
                    </div>
                  )
                })}
              </div>

              <div className="space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h4 className="text-sm font-semibold">
                    Provider connections
                  </h4>
                  <span className="text-xs text-muted-foreground">
                    Authority:{' '}
                    {setup.caller_capabilities.management_authority}
                  </span>
                </div>
                {setup.connections.length === 0 ? (
                  <p className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                    No provider connection is configured.
                  </p>
                ) : (
                  <div className="divide-y rounded-md border">
                    {setup.connections.map((connection) => (
                      <div
                        key={connection.provider}
                        className="flex flex-wrap items-center justify-between gap-2 p-3 text-sm"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">
                            {PROVIDER_LABELS[connection.provider]}
                          </span>
                          <Badge
                            variant={
                              connection.current
                                ? 'default'
                                : connection.state === 'revoked'
                                  ? 'secondary'
                                  : 'destructive'
                            }
                          >
                            {connection.current
                              ? 'current'
                              : connection.state}
                          </Badge>
                        </div>
                        <span className="text-xs text-muted-foreground">
                          Connection v{connection.connection_version} ·
                          inventory v{connection.inventory_version} · validated{' '}
                          <RelativeTime value={connection.validated_at} />
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {setup.blockers.filter(
                (blocker) => blocker !== 'project_inactive',
              ).length > 0 ? (
                <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                  <p className="flex items-center gap-2 font-medium">
                    <CircleAlert className="h-4 w-4" />
                    Setup needs attention
                  </p>
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-xs">
                    {setup.blockers
                      .filter((blocker) => blocker !== 'project_inactive')
                      .map((blocker) => (
                        <li key={blocker}>{BLOCKER_LABELS[blocker]}</li>
                      ))}
                  </ul>
                </div>
              ) : null}

              <div className="flex flex-wrap items-center gap-2">
                {setup.caller_capabilities.can_manage ? (
                  <Button
                    type="button"
                    size="sm"
                    onClick={() => setWizardOpen(true)}
                  >
                    <Settings2 />
                    {setup.state === 'inactive'
                      ? 'Set up Agentic runs'
                      : 'Manage setup'}
                  </Button>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    Setup is read-only. Management authority:{' '}
                    {setup.caller_capabilities.management_authority}.
                  </p>
                )}
                {setup.caller_capabilities.can_deactivate ? (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => setDeactivateOpen(true)}
                  >
                    <PauseCircle />
                    Deactivate
                  </Button>
                ) : null}
                {setup.activated_at ? (
                  <span className="text-xs text-muted-foreground">
                    Activated <RelativeTime value={setup.activated_at} />
                  </span>
                ) : null}
              </div>
            </>
          ) : null}
        </CardContent>
      </Card>

      {setup && wizardOpen ? (
        <AgentsSetupWizard
          setup={setup}
          open={wizardOpen}
          onClose={() => setWizardOpen(false)}
          onSetupChanged={async () => {
            await invalidateSetup()
          }}
        />
      ) : null}

      <Dialog
        open={deactivateOpen}
        onOpenChange={(open) => {
          if (!open && !pending) {
            setDeactivateOpen(false)
            setDeactivationReason('')
            setConfirmed(false)
            setDeactivationError(null)
          }
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => void deactivate(event)}
            className="space-y-4"
            noValidate
          >
            <DialogHeader>
              <DialogTitle>Deactivate Agentic runs?</DialogTitle>
              <DialogDescription>
                New analysis runs will be rejected. Existing history remains
                readable, and provider connections stay stored for later
                reactivation. A provider request that already crossed the
                egress boundary may still complete.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="agents-deactivation-reason">Reason</Label>
              <Textarea
                id="agents-deactivation-reason"
                value={deactivationReason}
                maxLength={2_000}
                disabled={pending}
                onChange={(event) =>
                  setDeactivationReason(event.target.value)
                }
                placeholder="Why are Agentic runs being deactivated?"
              />
            </div>
            <label className="flex items-start gap-2 rounded-md border p-3 text-sm">
              <input
                type="checkbox"
                checked={confirmed}
                disabled={pending}
                onChange={(event) => setConfirmed(event.target.checked)}
                className="mt-0.5"
              />
              <span>
                I understand this immediately blocks new agent run admission
                for project <code className="font-mono">{projectId}</code>.
              </span>
            </label>
            {deactivationError ? (
              <p className="text-sm text-destructive" role="alert">
                {deactivationError}
              </p>
            ) : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => setDeactivateOpen(false)}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="destructive"
                disabled={
                  pending || !confirmed || !deactivationReason.trim()
                }
              >
                {pending ? <Loader2 className="animate-spin" /> : null}
                Deactivate Agentic runs
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
