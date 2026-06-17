import { useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { ApiError } from '@/api/http'
import type { ExperimentUpdate } from '@/api/types/experiments'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  buildCreate,
  buildUpdate,
  emptyExperimentValues,
  entryToFormValues,
  ExperimentForm,
  type ExperimentFormValues,
} from '@/features/experiments/ExperimentForm'
import {
  useCreateExperimentMutation,
  useDeleteExperimentMutation,
  useExperimentsQuery,
  useUpdateExperimentMutation,
} from '@/features/experiments/hooks'
import { ExperimentResultsTab } from '@/features/experiments/ExperimentResultsTab'
import { ExperimentStatusPill } from '@/features/experiments/StatusPill'

export function ExperimentDetailPage() {
  const { key = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const experimentsQuery = useExperimentsQuery()
  const updateMutation = useUpdateExperimentMutation(key)
  const deleteMutation = useDeleteExperimentMutation(key)

  const tab = searchParams.get('tab') === 'setup' ? 'setup' : 'results'
  const entry = experimentsQuery.data?.experiments.find((experiment) => experiment.key === key)

  const [values, setValues] = useState<ExperimentFormValues | null>(null)
  const [loadedUpdatedAt, setLoadedUpdatedAt] = useState<string | null>(null)
  const [staleConfirm, setStaleConfirm] = useState<{ pending: ExperimentUpdate; serverUpdatedAt: string } | null>(null)
  const [deleteOpen, setDeleteOpen] = useState(false)

  if (entry && values === null) {
    setValues(entryToFormValues(entry))
    setLoadedUpdatedAt(entry.updated_at)
  }

  const submitUpdate = async (body: ExperimentUpdate) => {
    try {
      await updateMutation.mutateAsync(body)
      toast.success(`Experiment "${key}" saved`)
      const refreshed = await experimentsQuery.refetch()
      const current = refreshed.data?.experiments.find((experiment) => experiment.key === key)
      if (current) {
        setValues(entryToFormValues(current))
        setLoadedUpdatedAt(current.updated_at)
      }
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : 'Save failed')
    }
  }

  const save = async () => {
    if (!values) return
    const body = buildUpdate(values)
    // No optimistic locking on experiments — pre-empt silent last-write-wins
    // by re-checking updated_at before submitting (plan §5.4.2).
    const refreshed = await experimentsQuery.refetch()
    const current = refreshed.data?.experiments.find((experiment) => experiment.key === key)
    if (current && loadedUpdatedAt !== null && current.updated_at !== loadedUpdatedAt) {
      setStaleConfirm({ pending: body, serverUpdatedAt: current.updated_at })
      return
    }
    await submitUpdate(body)
  }

  if (experimentsQuery.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }
  if (experimentsQuery.error) {
    return <ErrorState error={experimentsQuery.error} onRetry={() => void experimentsQuery.refetch()} />
  }
  if (!entry) {
    return (
      <EmptyState
        title={`Experiment "${key}" not found`}
        description="It may have been deleted — the list refreshes automatically."
      />
    )
  }

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/experiments', label: 'Experiments' }}
        title={
          <span className="flex flex-wrap items-center gap-2">
            <code className="font-mono">{entry.key}</code>
            <ExperimentStatusPill status={entry.status} />
          </span>
        }
        description={
          <>
            {entry.description || 'No description'} · updated{' '}
            <RelativeTime value={entry.updated_at} />
          </>
        }
        actions={
          <Button variant="destructive" size="sm" onClick={() => setDeleteOpen(true)}>
            Delete…
          </Button>
        }
      />

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setSearchParams(
            (previous) => {
              const next = new URLSearchParams(previous)
              if (value === 'results') next.delete('tab')
              else next.set('tab', value)
              return next
            },
            { replace: true },
          )
        }
      >
        <TabsList>
          <TabsTrigger value="results">Results</TabsTrigger>
          <TabsTrigger value="setup">Setup</TabsTrigger>
        </TabsList>
        <TabsContent value="results">
          <ExperimentResultsTab experimentKey={entry.key} defaultFlagKey={entry.flag_key} />
        </TabsContent>
        <TabsContent value="setup">
          {values ? (
            <ExperimentForm
              values={values}
              onChange={setValues}
              isCreate={false}
              currentStatus={entry.status}
              onSubmit={() => void save()}
              submitting={updateMutation.isPending}
            />
          ) : null}
        </TabsContent>
      </Tabs>

      <Dialog open={staleConfirm !== null} onOpenChange={(open) => !open && setStaleConfirm(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Experiment changed since you loaded it</DialogTitle>
            <DialogDescription>
              It was updated <RelativeTime value={staleConfirm?.serverUpdatedAt ?? null} /> by
              someone else. Experiments have no version locking — saving now overwrites their
              changes (last write wins).
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setStaleConfirm(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                const pending = staleConfirm?.pending
                setStaleConfirm(null)
                if (pending) void submitUpdate(pending)
              }}
            >
              Overwrite anyway
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete experiment "{entry.key}"?</DialogTitle>
            <DialogDescription>
              Removes the experiment record. Exposure events and statistics history in ClickHouse
              are not touched.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                void deleteMutation
                  .mutateAsync()
                  .then(() => {
                    toast.success(`Experiment "${entry.key}" deleted`)
                    navigate('/experiments')
                  })
                  .catch((error: unknown) =>
                    toast.error(error instanceof ApiError ? error.message : 'Delete failed'),
                  )
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

export function ExperimentCreatePage() {
  const navigate = useNavigate()
  const createMutation = useCreateExperimentMutation()
  const [values, setValues] = useState<ExperimentFormValues>(emptyExperimentValues)
  const [keyError, setKeyError] = useState<string | null>(null)

  const submit = async () => {
    setKeyError(null)
    try {
      const response = await createMutation.mutateAsync(buildCreate(values))
      toast.success(`Experiment "${response.key}" created`)
      navigate(`/experiments/${encodeURIComponent(response.key)}?tab=setup`)
    } catch (error) {
      if (error instanceof ApiError && error.code === 'conflict') setKeyError(error.message)
      else toast.error(error instanceof ApiError ? error.message : 'Create failed')
    }
  }

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/experiments', label: 'Experiments' }}
        title="Create experiment"
        description="Creating an experiment also creates its backing flag — start it as running to begin bucketing users immediately."
      />
      <ExperimentForm
        values={values}
        onChange={setValues}
        isCreate
        onSubmit={() => void submit()}
        submitting={createMutation.isPending}
        keyError={keyError}
      />
    </div>
  )
}

