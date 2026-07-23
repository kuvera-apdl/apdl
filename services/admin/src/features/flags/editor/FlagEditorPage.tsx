// Flag editor (plan §5.3.3): one form, two modes. Mirrors FlagCreate /
// FlagUpdate exactly; edits go through a pre-submit review sheet and a
// version-conflict rebase dialog.
import { zodResolver } from '@hookform/resolvers/zod'
import { FlaskConical } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Controller, FormProvider, useForm } from 'react-hook-form'
import { useBlocker, useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { createFlagCurl, updateFlagCurl } from '@/api/config'
import { ApiError } from '@/api/http'
import type { FlagConfig, FlagCreate, FlagUpdate } from '@/api/types/flags'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { TagInput } from '@/components/shared/TagInput'
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
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'
import { useFlagsQuery } from '@/features/flags/hooks'
import { useCreateFlagMutation, useUpdateFlagMutation } from '@/features/flags/mutations'
import { PopulationSimulator } from '@/features/flags/tester/PopulationSimulator'
import type { CurlSpec } from '@/lib/curl'

import { ConflictDialog } from './ConflictDialog'
import {
  emptyFormValues,
  flagFormSchema,
  flagToFormValues,
  formToCreatePayload,
  formToEvaluable,
  formToUpdatePlan,
  type FlagFormValues,
} from './formModel'
import { GuardrailsEditor } from './GuardrailsEditor'
import { ReviewSheet } from './ReviewSheet'
import { RolloutFields } from './RolloutFields'
import { RuleBuilder } from './RuleBuilder'
import { VariantsEditor } from './VariantsEditor'

const WRITABLE_STATES = [
  { value: 'draft', label: 'Draft', hint: 'not served — safe to iterate' },
  { value: 'active', label: 'Active', hint: 'evaluating and serving traffic' },
  { value: 'disabled', label: 'Disabled', hint: 'off — SDKs fall back to defaults' },
] as const

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description ? <CardDescription>{description}</CardDescription> : null}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

export function FlagEditorPage() {
  const { key } = useParams()
  const { active } = useWorkspace()
  if (!hasWorkspaceRole(active, 'config:write')) {
    return (
      <EmptyState
        title="Flag editing unavailable"
        description="Creating or editing flags requires config:write for the active project."
      />
    )
  }

  return <FlagEditor key={`${active?.id ?? 'no-workspace'}:${key ?? '__new__'}`} flagKey={key} />
}

function FlagEditor({ flagKey: key }: { flagKey: string | undefined }) {
  const isEdit = key !== undefined
  const navigate = useNavigate()
  const { active } = useWorkspace()
  const conn = active ? serviceConnection(active, 'config') : null
  const flagsQuery = useFlagsQuery()
  const base = isEdit ? flagsQuery.data?.flags.find((flag) => flag.key === key) : undefined

  const form = useForm<FlagFormValues>({
    resolver: zodResolver(flagFormSchema),
    defaultValues: emptyFormValues(),
    mode: 'onBlur',
  })
  const { register, handleSubmit, control, watch, setValue, formState } = form

  // The version the form was loaded against — never silently re-synced.
  const baseRef = useRef<FlagConfig | null>(null)
  const [serverMoved, setServerMoved] = useState(false)
  const leavingRef = useRef(false)

  useEffect(() => {
    if (!isEdit || !base) return
    if (baseRef.current === null) {
      baseRef.current = base
      form.reset(flagToFormValues(base))
    } else if (base.version !== baseRef.current.version) {
      // SSE-driven refetch noticed a concurrent change — warn, don't clobber.
      setServerMoved(true)
    }
  }, [isEdit, base, form])

  const createMutation = useCreateFlagMutation()
  const updateMutation = useUpdateFlagMutation(key ?? '')

  const [review, setReview] = useState<{ payload: FlagCreate | FlagUpdate; curl: CurlSpec } | null>(null)
  const [reviewError, setReviewError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<{ current: FlagConfig | null; pending: FlagUpdate } | null>(null)
  const [simulateOpen, setSimulateOpen] = useState(false)

  const state = watch('state')

  const prepareReview = (values: FlagFormValues) => {
    if (!conn) return
    setReviewError(null)
    if (isEdit) {
      const loaded = baseRef.current
      if (!loaded) return
      const plan = formToUpdatePlan(values, loaded, loaded.version)
      if (plan.changedFields.length === 0) {
        toast.info('No changes to save')
        return
      }
      setReview({ payload: plan.payload, curl: updateFlagCurl(conn, loaded.key, plan.payload) })
    } else {
      const payload = formToCreatePayload(values)
      setReview({ payload, curl: createFlagCurl(conn, payload) })
    }
  }

  const confirmSubmit = async () => {
    if (!review) return
    try {
      if (isEdit) {
        const response = await updateMutation.mutateAsync(review.payload as FlagUpdate)
        leavingRef.current = true
        toast.success(`Saved "${response.flag.key}" — now v${response.flag.version}`)
        navigate(`/flags/${encodeURIComponent(response.flag.key)}`)
      } else {
        const response = await createMutation.mutateAsync(review.payload as FlagCreate)
        leavingRef.current = true
        toast.success(`Flag "${response.flag.key}" created`)
        navigate(`/flags/${encodeURIComponent(response.flag.key)}`)
      }
    } catch (error) {
      if (isEdit && error instanceof ApiError && error.code === 'version_conflict') {
        const pending = review.payload as FlagUpdate
        setReview(null)
        const refreshed = await flagsQuery.refetch()
        const current = refreshed.data?.flags.find((flag) => flag.key === key) ?? null
        setConflict({ current, pending })
      } else if (!isEdit && error instanceof ApiError && error.code === 'conflict') {
        setReview(null)
        form.setError('key', { message: error.message })
      } else {
        setReviewError(error instanceof Error ? error.message : 'Request failed')
      }
    }
  }

  const rebaseOntoCurrent = () => {
    if (!conflict?.current) return
    baseRef.current = conflict.current
    setConflict(null)
    setServerMoved(false)
    prepareReview(form.getValues())
  }

  const discardMine = () => {
    if (conflict?.current) {
      baseRef.current = conflict.current
      form.reset(flagToFormValues(conflict.current))
    }
    setConflict(null)
    setServerMoved(false)
  }

  const reloadFromServer = () => {
    if (!base) return
    baseRef.current = base
    form.reset(flagToFormValues(base))
    setServerMoved(false)
  }

  const openSimulator = async () => {
    const valid = await form.trigger()
    if (!valid) {
      toast.error('Fix validation errors before simulating')
      return
    }
    setSimulateOpen(true)
  }

  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      formState.isDirty && !leavingRef.current && currentLocation.pathname !== nextLocation.pathname,
  )

  if (isEdit && flagsQuery.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }
  if (isEdit && flagsQuery.error) {
    return <ErrorState error={flagsQuery.error} onRetry={() => void flagsQuery.refetch()} />
  }
  if (isEdit && !base) {
    return (
      <EmptyState
        title={`Flag "${key}" not found`}
        description="It may have been archived or deleted — archived flags cannot be edited."
      />
    )
  }
  if (isEdit && base?.state === 'archived') {
    return (
      <EmptyState
        title="Archived flags cannot be edited"
        description="Archive is a terminal state. Create a new flag if this behavior is needed again."
      />
    )
  }

  const quickSetReviewBy = () => {
    const date = new Date()
    date.setDate(date.getDate() + 90)
    setValue('review_by', date.toISOString().slice(0, 10), { shouldDirty: true, shouldValidate: true })
  }

  return (
    <FormProvider {...form}>
      <div className="space-y-5">
        <PageHeader
          backTo={
            isEdit
              ? { to: `/flags/${encodeURIComponent(key)}`, label: key }
              : { to: '/flags', label: 'Flags' }
          }
          title={isEdit ? `Edit ${key}` : 'Create flag'}
          description={
            isEdit && baseRef.current
              ? `Editing v${baseRef.current.version} as ${active?.actor ?? 'admin'}`
              : `Creating as ${active?.actor ?? 'admin'}`
          }
          actions={
            <>
              <Button type="button" variant="outline" onClick={openSimulator}>
                <FlaskConical />
                Simulate
              </Button>
              <Button type="button" onClick={handleSubmit(prepareReview)}>
                Review & save
              </Button>
            </>
          }
        />

        {serverMoved && base ? (
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-900 dark:bg-amber-950/30">
            <span>
              This flag changed to v{base.version} while you were editing — saving will hit a
              version conflict.
            </span>
            <Button type="button" variant="outline" size="sm" onClick={reloadFromServer}>
              Discard my edits & reload v{base.version}
            </Button>
          </div>
        ) : null}

        <form className="space-y-4" onSubmit={handleSubmit(prepareReview)} noValidate>
          <Section title="Identity">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Key</Label>
                <Input
                  {...register('key')}
                  disabled={isEdit}
                  placeholder="checkout-cta"
                  className="font-mono text-xs"
                />
                {formState.errors.key ? (
                  <p className="text-xs text-destructive">{formState.errors.key.message}</p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {isEdit ? 'Immutable after creation.' : 'Permanent identifier — choose carefully.'}
                  </p>
                )}
              </div>
              <div className="space-y-1.5">
                <Label>Name</Label>
                <Input {...register('name')} placeholder="Checkout CTA experiment" />
                {formState.errors.name ? (
                  <p className="text-xs text-destructive">{formState.errors.name.message}</p>
                ) : null}
              </div>
            </div>
            <div className="mt-4 space-y-1.5">
              <Label>Description</Label>
              <Input {...register('description')} placeholder="What this flag controls" />
            </div>
            <div className="mt-4 grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>Owners</Label>
                <Controller
                  control={control}
                  name="owners"
                  render={({ field }) => (
                    <TagInput
                      value={field.value}
                      onChange={field.onChange}
                      placeholder="add owner, press Enter"
                      aria-label="Owners"
                    />
                  )}
                />
              </div>
              <div className="space-y-1.5">
                <Label>Review by</Label>
                <div className="flex items-center gap-2">
                  <Input type="date" className="w-44" {...register('review_by')} />
                  <Button type="button" variant="outline" size="sm" onClick={quickSetReviewBy}>
                    +90 days
                  </Button>
                </div>
                {formState.errors.review_by ? (
                  <p className="text-xs text-destructive">{formState.errors.review_by.message}</p>
                ) : isEdit && baseRef.current?.review_by ? (
                  <p className="text-xs text-muted-foreground">
                    The API cannot clear a review date — emptying this field leaves it unchanged.
                  </p>
                ) : null}
              </div>
            </div>
          </Section>

          <Section
            title="State"
            description={
              isEdit
                ? 'Existing flag lifecycle changes use the dedicated actions on the flag detail page.'
                : 'enabled is derived: a flag is enabled exactly when state is active.'
            }
          >
            {isEdit ? (
              <p className="text-sm">
                Current state: <code className="font-mono">{base?.state}</code>. Save configuration
                edits here, then use Activate, Deactivate, Disable, or Archive from the flag detail
                page for a version-checked lifecycle change.
              </p>
            ) : (
              <>
                <div className="flex flex-wrap gap-3">
                  {WRITABLE_STATES.map((option) => (
                    <label
                      key={option.value}
                      className="flex cursor-pointer items-start gap-2 rounded-md border p-3 has-[:checked]:border-foreground"
                    >
                      <input type="radio" value={option.value} {...register('state')} className="mt-1 accent-foreground" />
                      <span>
                        <span className="block text-sm font-medium">{option.label}</span>
                        <span className="block text-xs text-muted-foreground">{option.hint}</span>
                      </span>
                    </label>
                  ))}
                </div>
                <p className="mt-3 text-xs text-muted-foreground">
                  Derived: <code className="font-mono">enabled = {String(state === 'active')}</code>
                </p>
              </>
            )}
          </Section>

          <Section title="Variants">
            <VariantsEditor />
          </Section>

          <Section title="Targeting rules" description="Evaluated top-down; first full match wins.">
            <RuleBuilder />
          </Section>

          <Section title="Fallthrough" description="Applies to users who miss every rule.">
            <RolloutFields
              pathPrefix="fallthrough.rollout"
              percentageError={formState.errors.fallthrough?.rollout?.percentage?.message}
              bucketByError={formState.errors.fallthrough?.rollout?.bucket_by?.message}
            />
          </Section>

          <Section title="Safety">
            <div className="space-y-5">
              <div className="max-w-xs space-y-1.5">
                <Label>Evaluation mode</Label>
                <Select {...register('evaluation_mode')}>
                  <option value="client">client — SSE-distributed to browsers</option>
                  <option value="server">server — evaluated via /v1/evaluate only</option>
                  <option value="both">both — distributed and server-evaluable</option>
                </Select>
              </div>
              <GuardrailsEditor />
            </div>
          </Section>
        </form>

        <ReviewSheet
          open={review !== null}
          onOpenChange={(open) => {
            if (!open) setReview(null)
          }}
          title={isEdit ? `Update ${key}` : `Create ${watch('key') || 'flag'}`}
          description={
            isEdit
              ? `Only changed fields are sent, with version ${baseRef.current?.version ?? '?'} for optimistic locking.`
              : 'The canonical FlagCreate payload — the salt is generated server-side.'
          }
          payload={review?.payload ?? null}
          curl={review?.curl ?? { method: 'POST', url: '' }}
          error={reviewError}
          submitting={createMutation.isPending || updateMutation.isPending}
          confirmLabel={isEdit ? 'Save changes' : 'Create flag'}
          onConfirm={() => void confirmSubmit()}
        />

        {isEdit && baseRef.current ? (
          <ConflictDialog
            open={conflict !== null}
            baseFlag={baseRef.current}
            currentFlag={conflict?.current ?? null}
            pendingUpdate={conflict?.pending ?? { version: 0 }}
            onRebase={rebaseOntoCurrent}
            onDiscard={discardMine}
            onClose={() => setConflict(null)}
          />
        ) : null}

        <Dialog open={simulateOpen} onOpenChange={setSimulateOpen}>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle>Population simulation — unsaved config</DialogTitle>
              <DialogDescription>
                10,000 synthetic users through this exact configuration (treated as active).
                {!isEdit
                  ? ' Creates use a preview salt: shares are representative, but per-user assignments will differ once the server generates the real salt.'
                  : ''}
              </DialogDescription>
            </DialogHeader>
            {simulateOpen ? (
              <PopulationSimulator
                flag={formToEvaluable(form.getValues(), {
                  salt: baseRef.current?.salt,
                  version: baseRef.current?.version,
                })}
              />
            ) : null}
          </DialogContent>
        </Dialog>

        <Dialog open={blocker.state === 'blocked'} onOpenChange={(open) => !open && blocker.reset?.()}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Discard unsaved changes?</DialogTitle>
              <DialogDescription>This flag has edits that have not been saved.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => blocker.reset?.()}>
                Keep editing
              </Button>
              <Button variant="destructive" onClick={() => blocker.proceed?.()}>
                Discard changes
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    </FormProvider>
  )
}
