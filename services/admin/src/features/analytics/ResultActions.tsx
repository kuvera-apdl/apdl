// Shared per-result affordances (plan §5.5.1): copy-as-curl, raw JSON drawer,
// CSV export of the rendered table.
import { Braces, Download } from 'lucide-react'

import { CurlButton } from '@/components/shared/CurlButton'
import { JsonView } from '@/components/shared/JsonView'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import type { CurlSpec } from '@/lib/curl'
import { downloadCsv, type CsvCell } from '@/lib/csv'

interface ResultActionsProps {
  curl: CurlSpec | null
  raw: unknown
  csv?: { filename: string; headers: string[]; rows: CsvCell[][] } | null
}

export function ResultActions({ curl, raw, csv }: ResultActionsProps) {
  return (
    <div className="flex items-center gap-2">
      {curl ? <CurlButton spec={curl} title="Query as curl" /> : null}
      {raw !== undefined && raw !== null ? (
        <Dialog>
          <DialogTrigger asChild>
            <Button variant="outline" size="sm">
              <Braces />
              JSON
            </Button>
          </DialogTrigger>
          <DialogContent className="max-w-2xl">
            <DialogHeader>
              <DialogTitle>Raw response</DialogTitle>
              <DialogDescription>Exactly what the query service returned.</DialogDescription>
            </DialogHeader>
            <JsonView data={raw} className="max-h-[60vh]" />
          </DialogContent>
        </Dialog>
      ) : null}
      {csv ? (
        <Button
          variant="outline"
          size="sm"
          onClick={() => downloadCsv(csv.filename, csv.headers, csv.rows)}
        >
          <Download />
          CSV
        </Button>
      ) : null}
    </div>
  )
}
