// Create/edit wizard for custom agents: Basics → Prompts → Data tools →
// Behavior → Test & save. Per-step validation gates Next; the final step can
// dry-run the draft against real project data (POST /custom/test) before
// anything is persisted.
//
// Custom agents are agentic: the model calls the read-only data tools itself
// (picking parameters at run time) inside a bounded loop. The Data tools step
// therefore selects which tools are ALLOWED — default all — not pre-baked
// queries.
import { ArrowLeft, ArrowRight, Check, FlaskConical, Save } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import {
  createCustomAgent,
  createCustomAgentCurl,
  getCustomAgent,
  listAgentDefinitions,
  testCustomAgent,
  updateCustomAgent,
} from '@/api/agents'
import { ApiError } from '@/api/http'
import { customAgentSpecSchema } from '@/api/schemas/agents'
import type {
  AgentDefinitionsResponse,
  CustomAgentSpec,
  TestRunResponse,
  ToolCatalogEntry,
} from '@/api/types/agents'
import { CurlButton } from '@/components/shared/CurlButton'
import { JsonView } from '@/components/shared/JsonView'
import { PageHeader } from '@/components/shared/PageHeader'
import { ErrorState } from '@/components/shared/PanelStates'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Textarea } from '@/components/ui/textarea'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { cn } from '@/lib/utils'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const STEPS = ['Basics', 'Prompts', 'Data tools', 'Behavior', 'Test & save'] as const

const SLUG_RE = /^[a-z][a-z0-9_]{2,63}$/

// Base placeholders always available to the user prompt template; one more
// per selected `requires` key (documented inline in the Prompts step).
const BASE_PLACEHOLDERS = ['{context}', '{project_id}', '{time_range_days}']

interface FormState {
  slug: string
  slugTouched: boolean
  display_name: string
  description: string
  system_prompt: string
  user_prompt_template: string
  model_tier: 'fast' | 'reasoning'
  /** UI-only: false = whole catalog allowed (spec sends []); true = subset. */
  limitTools: boolean
  /** Allowed tool names; only meaningful when limitTools is true. */
  tools: string[]
  max_tool_steps: number
  requires: string[]
  produces: string
  memory_query: string
  memory_top_k: number
  pipeline_order: number
}

const EMPTY_FORM: FormState = {
  slug: '',
  slugTouched: false,
  display_name: '',
  description: '',
  system_prompt: '',
  user_prompt_template:
    'Investigate project {project_id} over the last {time_range_days} days.\n\nRelevant past learnings:\n{context}\n\nUse your data tools to gather the evidence you need, then respond with a JSON array of findings.',
  model_tier: 'reasoning',
  limitTools: false,
  tools: [],
  max_tool_steps: 8,
  requires: [],
  produces: '',
  memory_query: '',
  memory_top_k: 5,
  pipeline_order: 100,
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^[^a-z]+/, '')
    .replace(/_+$/, '')
    .slice(0, 64)
}

function toSpec(form: FormState): CustomAgentSpec {
  return {
    slug: form.slug,
    display_name: form.display_name,
    description: form.description,
    system_prompt: form.system_prompt,
    user_prompt_template: form.user_prompt_template,
    model_tier: form.model_tier,
    // Empty = whole catalog allowed (the server default).
    tools: form.limitTools ? form.tools : [],
    max_tool_steps: form.max_tool_steps,
    requires: form.requires,
    produces: form.produces,
    memory_query: form.memory_query.trim() === '' ? null : form.memory_query,
    memory_top_k: form.memory_top_k,
    pipeline_order: form.pipeline_order,
  }
}

/** Per-step validation problems; empty list means the step is complete. */
function stepProblems(step: number, form: FormState): string[] {
  const problems: string[] = []
  if (step === 0) {
    if (form.display_name.trim() === '') problems.push('Name is required.')
    if (!SLUG_RE.test(form.slug))
      problems.push('Slug must be 3-64 lowercase letters, digits or underscores, starting with a letter.')
    if (form.description.length > 500) problems.push('Description is limited to 500 characters.')
  }
  if (step === 1) {
    if (form.system_prompt.trim() === '') problems.push('System prompt is required.')
    if (form.user_prompt_template.trim() === '') problems.push('User prompt template is required.')
  }
  if (step === 2) {
    if (form.limitTools && form.tools.length === 0)
      problems.push('Select at least one allowed tool, or switch back to allowing all tools.')
    if (form.max_tool_steps < 1 || form.max_tool_steps > 16)
      problems.push('Tool budget must be between 1 and 16 rounds.')
  }
  if (step === 3) {
    if (!SLUG_RE.test(form.produces))
      problems.push('Output key must be 3-64 lowercase letters, digits or underscores.')
    if (form.pipeline_order < 0 || form.pipeline_order > 1000)
      problems.push('Pipeline order must be between 0 and 1000.')
    if (form.memory_top_k < 1 || form.memory_top_k > 20)
      problems.push('Memory entries must be between 1 and 20.')
  }
  return problems
}

export function CustomAgentWizardPage() {
  const { agentId } = useParams<{ agentId: string }>()
  const isEdit = Boolean(agentId)
  const { active, projectId } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const [step, setStep] = useState(0)
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [showProblems, setShowProblems] = useState(false)
  const [testResult, setTestResult] = useState<TestRunResponse | null>(null)

  const conn = active ? serviceConnection(active, 'agents') : null

  const definitionsQuery = useQuery({
    queryKey:
      active && projectId ? queryKeys.agentDefinitions(active.id, projectId) : ['agent-defs-idle'],
    enabled: Boolean(conn && projectId),
    queryFn: ({ signal }) => listAgentDefinitions(conn!, projectId!, { signal }),
  })

  const existingQuery = useQuery({
    queryKey:
      active && projectId && agentId
        ? queryKeys.customAgent(active.id, projectId, agentId)
        : ['custom-agent-idle'],
    enabled: Boolean(conn && projectId && agentId),
    queryFn: ({ signal }) => getCustomAgent(conn!, projectId!, agentId!, { signal }),
  })

  // Prefill once when editing; keyed on agent_id so a route change re-seeds.
  useEffect(() => {
    const agent = existingQuery.data
    if (!agent) return
    setForm({
      slug: agent.slug,
      slugTouched: true,
      display_name: agent.display_name,
      description: agent.description,
      system_prompt: agent.system_prompt,
      user_prompt_template: agent.user_prompt_template,
      model_tier: agent.model_tier,
      limitTools: agent.tools.length > 0,
      tools: agent.tools,
      max_tool_steps: agent.max_tool_steps,
      requires: agent.requires,
      produces: agent.produces,
      memory_query: agent.memory_query ?? '',
      memory_top_k: agent.memory_top_k,
      pipeline_order: agent.pipeline_order,
    })
  }, [existingQuery.data])

  const update = (patch: Partial<FormState>) => setForm((prev) => ({ ...prev, ...patch }))

  const problems = stepProblems(step, form)
  const allProblems = useMemo(
    () => STEPS.flatMap((_, index) => stepProblems(index, form)),
    [form],
  )

  const saveMutation = useMutation({
    mutationFn: (spec: CustomAgentSpec) =>
      isEdit
        ? updateCustomAgent(conn!, projectId!, agentId!, spec)
        : createCustomAgent(conn!, projectId!, spec),
    onSuccess: (agent) => {
      toast.success(`Custom agent "${agent.display_name}" ${isEdit ? 'saved' : 'created'}`)
      if (active) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.customAgentsPrefix(active.id) })
      }
      navigate('/agents/custom')
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Save failed')
    },
  })

  const testMutation = useMutation({
    mutationFn: () =>
      testCustomAgent(conn!, {
        project_id: projectId!,
        time_range_days: 7,
        definition: toSpec(form),
      }),
    onSuccess: (result) => setTestResult(result),
    onError: (error) => {
      setTestResult(null)
      toast.error(error instanceof ApiError ? error.message : 'Test run failed')
    },
  })

  const submit = () => {
    const spec = toSpec(form)
    const parsed = customAgentSpecSchema.safeParse(spec)
    if (!parsed.success) {
      setShowProblems(true)
      toast.error('Fix the highlighted problems before saving.')
      return
    }
    saveMutation.mutate(parsed.data)
  }

  const next = () => {
    if (problems.length > 0) {
      setShowProblems(true)
      return
    }
    setShowProblems(false)
    setStep((value) => Math.min(value + 1, STEPS.length - 1))
  }

  if (isEdit && existingQuery.isError) {
    return <ErrorState error={existingQuery.error} onRetry={() => void existingQuery.refetch()} />
  }
  if (isEdit && existingQuery.isPending) {
    return (
      <div className="max-w-3xl space-y-3">
        <Skeleton className="h-8 w-1/2" />
        <Skeleton className="h-48 w-full" />
      </div>
    )
  }

  const definitions = definitionsQuery.data
  const spec = toSpec(form)

  return (
    <div className="max-w-3xl space-y-5">
      <PageHeader
        backTo={{ to: '/agents/custom', label: 'Custom agents' }}
        title={isEdit ? `Edit ${existingQuery.data?.display_name ?? 'custom agent'}` : 'New custom agent'}
        description="A read-only analysis agent: it investigates project data by calling the query tools you allow — choosing its own queries as it reasons — and stores its findings in run results. It never deploys or changes anything."
        actions={
          conn && projectId ? (
            <CurlButton
              spec={createCustomAgentCurl(conn, projectId, spec)}
              title={isEdit ? 'Update custom agent' : 'Create custom agent'}
            />
          ) : null
        }
      />

      <Stepper step={step} onStep={setStep} form={form} />

      {step === 0 ? <BasicsStep form={form} update={update} /> : null}
      {step === 1 ? <PromptsStep form={form} update={update} /> : null}
      {step === 2 ? (
        <ToolsStep form={form} update={update} definitions={definitions} />
      ) : null}
      {step === 3 ? (
        <BehaviorStep form={form} update={update} definitions={definitions} />
      ) : null}
      {step === 4 ? (
        <TestStep
          form={form}
          result={testResult}
          testing={testMutation.isPending}
          onTest={() => testMutation.mutate()}
          allProblems={allProblems}
        />
      ) : null}

      {showProblems && problems.length > 0 ? (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
          <ul className="list-inside list-disc">
            {problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="flex items-center justify-between">
        <Button variant="outline" disabled={step === 0} onClick={() => setStep((v) => v - 1)}>
          <ArrowLeft />
          Back
        </Button>
        {step < STEPS.length - 1 ? (
          <Button onClick={next}>
            Next
            <ArrowRight />
          </Button>
        ) : (
          <Button onClick={submit} disabled={saveMutation.isPending || allProblems.length > 0}>
            <Save />
            {isEdit ? 'Save changes' : 'Create agent'}
          </Button>
        )}
      </div>
    </div>
  )
}

function Stepper({
  step,
  onStep,
  form,
}: {
  step: number
  onStep: (step: number) => void
  form: FormState
}) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {STEPS.map((label, index) => {
        const complete = stepProblems(index, form).length === 0
        const current = index === step
        return (
          <li key={label} className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => onStep(index)}
              className={cn(
                'flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm transition-colors',
                current
                  ? 'border-foreground font-medium'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <span
                className={cn(
                  'flex h-4 w-4 items-center justify-center rounded-full text-[10px]',
                  complete && !current ? 'bg-emerald-600 text-white' : 'bg-secondary',
                )}
              >
                {complete && !current ? <Check className="h-3 w-3" /> : index + 1}
              </span>
              {label}
            </button>
            {index < STEPS.length - 1 ? (
              <span className="text-muted-foreground/50">—</span>
            ) : null}
          </li>
        )
      })}
    </ol>
  )
}

function BasicsStep({ form, update }: { form: FormState; update: (p: Partial<FormState>) => void }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Basics</CardTitle>
        <CardDescription>
          The slug is the agent&rsquo;s stable identifier — it appears in trigger requests and run
          phases.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="ca-name">Name</Label>
          <Input
            id="ca-name"
            value={form.display_name}
            maxLength={120}
            placeholder="Churn signal watch"
            onChange={(event) =>
              update({
                display_name: event.target.value,
                ...(form.slugTouched ? {} : { slug: slugify(event.target.value) }),
              })
            }
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ca-slug">Slug</Label>
          <Input
            id="ca-slug"
            value={form.slug}
            className="font-mono"
            placeholder="churn_signal_watch"
            onChange={(event) => update({ slug: event.target.value, slugTouched: true })}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ca-description">Description</Label>
          <Textarea
            id="ca-description"
            value={form.description}
            maxLength={500}
            rows={2}
            placeholder="What this agent looks for and why."
            onChange={(event) => update({ description: event.target.value })}
          />
        </div>
      </CardContent>
    </Card>
  )
}

function PromptsStep({ form, update }: { form: FormState; update: (p: Partial<FormState>) => void }) {
  const placeholders = [...BASE_PLACEHOLDERS, ...form.requires.map((key) => `{${key}}`)]
  return (
    <Card>
      <CardHeader>
        <CardTitle>Prompts</CardTitle>
        <CardDescription>
          Placeholders are substituted literally at run time; unknown braces (e.g. JSON examples)
          pass through untouched. Available: {placeholders.join(', ')}.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="ca-system">System prompt</Label>
          <Textarea
            id="ca-system"
            value={form.system_prompt}
            rows={5}
            className="font-mono text-xs"
            placeholder="You are a senior product analyst focused on…"
            onChange={(event) => update({ system_prompt: event.target.value })}
          />
        </div>
        <div className="space-y-1.5">
          <Label htmlFor="ca-user">User prompt template</Label>
          <Textarea
            id="ca-user"
            value={form.user_prompt_template}
            rows={10}
            className="font-mono text-xs"
            onChange={(event) => update({ user_prompt_template: event.target.value })}
          />
          {form.user_prompt_template.includes('{tool_results}') ? (
            <p className="text-xs text-amber-700 dark:text-amber-400">
              {'{tool_results}'} is a legacy placeholder and now renders empty — the agent calls
              its data tools itself while reasoning, so the template no longer needs it.
            </p>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

function ToolsStep({
  form,
  update,
  definitions,
}: {
  form: FormState
  update: (p: Partial<FormState>) => void
  definitions: AgentDefinitionsResponse | undefined
}) {
  const catalog = definitions?.tool_catalog ?? []
  const selected = new Set(form.tools)

  const toggle = (entry: ToolCatalogEntry) => {
    update({
      tools: selected.has(entry.name)
        ? form.tools.filter((name) => name !== entry.name)
        : [...form.tools, entry.name],
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Data tools</CardTitle>
        <CardDescription>
          The agent calls these read-only query tools itself while reasoning — it picks the
          queries and parameters at run time. Project and date window are always injected from
          the run, so it can never read outside its scope.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-1">
          {(
            [
              [false, 'All tools', 'the agent may use the entire catalog (default)'],
              [true, 'Limit tools', 'restrict the agent to a subset you pick below'],
            ] as const
          ).map(([limited, label, hint]) => (
            <label key={label} className="flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                name="limit-tools"
                checked={form.limitTools === limited}
                onChange={() => update({ limitTools: limited })}
                className="accent-foreground"
              />
              <span className="font-medium">{label}</span>
              <span className="text-xs text-muted-foreground">{hint}</span>
            </label>
          ))}
        </div>

        {catalog.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Tool catalog unavailable — is the agents service reachable?
          </p>
        ) : (
          <div className={cn('space-y-2', !form.limitTools && 'pointer-events-none opacity-50')}>
            {catalog.map((entry) => {
              const checked = !form.limitTools || selected.has(entry.name)
              return (
                <div
                  key={entry.name}
                  className={cn('rounded-md border p-3', checked && 'border-foreground')}
                >
                  <label className="flex cursor-pointer items-start gap-3">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={!form.limitTools}
                      onChange={() => toggle(entry)}
                      className="mt-1 accent-foreground"
                    />
                    <span>
                      <span className="block font-mono text-sm font-medium">{entry.name}</span>
                      <span className="block text-xs text-muted-foreground">
                        {entry.description}
                      </span>
                    </span>
                  </label>
                </div>
              )
            })}
          </div>
        )}

        <div className="space-y-1.5">
          <Label htmlFor="ca-tool-steps">Tool budget (rounds)</Label>
          <Input
            id="ca-tool-steps"
            type="number"
            min={1}
            max={16}
            value={form.max_tool_steps}
            className="w-24 tabular-nums"
            onChange={(event) => update({ max_tool_steps: Number(event.target.value) || 8 })}
          />
          <p className="text-xs text-muted-foreground">
            Maximum tool-calling rounds before the agent must produce its final answer. More
            rounds mean deeper investigation and higher LLM cost.
          </p>
        </div>
      </CardContent>
    </Card>
  )
}

function BehaviorStep({
  form,
  update,
  definitions,
}: {
  form: FormState
  update: (p: Partial<FormState>) => void
  definitions: AgentDefinitionsResponse | undefined
}) {
  // Upstream outputs this agent may depend on: every other agent's produces.
  const upstreamKeys = (definitions?.agents ?? [])
    .filter((agent) => agent.name !== form.slug)
    .map((agent) => ({ key: agent.produces, from: agent.name, order: agent.order }))

  const toggleRequire = (key: string) => {
    update({
      requires: form.requires.includes(key)
        ? form.requires.filter((k) => k !== key)
        : [...form.requires, key].slice(0, 5),
    })
  }

  const orderWarning = form.requires.some((key) => {
    const source = upstreamKeys.find((entry) => entry.key === key)
    return source !== undefined && source.order >= form.pipeline_order
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Behavior</CardTitle>
        <CardDescription>
          Where the agent sits in the pipeline, which model tier it reasons with, and what it
          consumes and produces.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label>Model tier</Label>
            <div className="space-y-1">
              {(['reasoning', 'fast'] as const).map((tier) => (
                <label key={tier} className="flex cursor-pointer items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="model-tier"
                    checked={form.model_tier === tier}
                    onChange={() => update({ model_tier: tier })}
                    className="accent-foreground"
                  />
                  <span className="font-mono">{tier}</span>
                  <span className="text-xs text-muted-foreground">
                    {tier === 'reasoning' ? 'deeper analysis, slower' : 'cheaper, quicker'}
                  </span>
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ca-produces">Output key (produces)</Label>
            <Input
              id="ca-produces"
              value={form.produces}
              className="font-mono"
              placeholder="churn_signals"
              onChange={(event) => update({ produces: event.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Where the agent&rsquo;s findings (always a JSON array) land in run results. Reserved
              keys (insights, experiment_designs, …) are rejected.
            </p>
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="ca-order">Pipeline order</Label>
            <Input
              id="ca-order"
              type="number"
              min={0}
              max={1000}
              value={form.pipeline_order}
              className="w-28 tabular-nums"
              onChange={(event) => update({ pipeline_order: Number(event.target.value) || 0 })}
            />
            <p className="text-xs text-muted-foreground">
              Lower runs earlier. Built-ins: behavior_analysis 10, experiment_design 20,
              personalization 30, feature_proposal 40.
            </p>
          </div>
        </div>

        <div className="space-y-1.5">
          <Label>Requires upstream outputs</Label>
          <p className="text-xs text-muted-foreground">
            The agent is skipped when a required key is empty at its turn. Each selected key also
            becomes a prompt placeholder.
          </p>
          <div className="space-y-1">
            {upstreamKeys.map((entry) => (
              <label
                key={`${entry.from}:${entry.key}`}
                className="flex cursor-pointer items-center gap-2 text-sm"
              >
                <input
                  type="checkbox"
                  checked={form.requires.includes(entry.key)}
                  onChange={() => toggleRequire(entry.key)}
                  className="accent-foreground"
                />
                <span className="font-mono">{entry.key}</span>
                <span className="text-xs text-muted-foreground">
                  from {entry.from} (order {entry.order})
                </span>
              </label>
            ))}
          </div>
          {orderWarning ? (
            <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              A required output is produced at or after this agent&rsquo;s pipeline order — it
              would always be empty. Increase the pipeline order.
            </p>
          ) : null}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="ca-memory">Memory query (optional)</Label>
            <Input
              id="ca-memory"
              value={form.memory_query}
              maxLength={500}
              placeholder="past churn findings and their outcomes"
              onChange={(event) => update({ memory_query: event.target.value })}
            />
            <p className="text-xs text-muted-foreground">
              Semantic search over the project&rsquo;s agent memory; results fill {'{context}'}.
            </p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="ca-topk">Memory entries</Label>
            <Input
              id="ca-topk"
              type="number"
              min={1}
              max={20}
              value={form.memory_top_k}
              className="w-24 tabular-nums"
              disabled={form.memory_query.trim() === ''}
              onChange={(event) => update({ memory_top_k: Number(event.target.value) || 5 })}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

function TestStep({
  form,
  result,
  testing,
  onTest,
  allProblems,
}: {
  form: FormState
  result: TestRunResponse | null
  testing: boolean
  onTest: () => void
  allProblems: string[]
}) {
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Test run</CardTitle>
          <CardDescription>
            Runs the draft&rsquo;s full agentic loop against real project data — the model calls
            its tools and answers — without saving anything. Costs up to the tool budget in model
            calls.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {allProblems.length > 0 ? (
            <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              <p className="mb-1 font-medium">Resolve before testing or saving:</p>
              <ul className="list-inside list-disc">
                {allProblems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <Button onClick={onTest} disabled={testing || allProblems.length > 0} variant="secondary">
            <FlaskConical />
            {testing ? 'Running…' : 'Run test'}
          </Button>
        </CardContent>
      </Card>

      {result ? (
        <Card>
          <CardHeader>
            <CardTitle>Test result</CardTitle>
            <CardDescription>
              agentic loop {result.timings_ms.llm ?? 0}ms · total {result.timings_ms.total ?? 0}ms
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <section className="space-y-1">
              <h3 className="text-sm font-medium">
                Parsed output{' '}
                <Badge variant="secondary" className="ml-1 font-mono">
                  {form.produces || 'output'}
                </Badge>
              </h3>
              <JsonView data={result.parsed_output} />
            </section>
            {result.tool_results.length > 0 ? (
              <section className="space-y-1">
                <h3 className="text-sm font-medium">
                  Tool calls <Badge variant="secondary">{result.tool_results.length}</Badge>
                </h3>
                <JsonView data={result.tool_results} />
              </section>
            ) : null}
            <details>
              <summary className="cursor-pointer text-sm text-muted-foreground">
                Rendered prompt & raw response
              </summary>
              <div className="mt-2 space-y-2">
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">
                  {result.prompt}
                </pre>
                <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">
                  {result.raw_response}
                </pre>
              </div>
            </details>
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
