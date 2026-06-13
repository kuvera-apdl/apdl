import { Terminal } from 'lucide-react'

import { CopyButton } from '@/components/shared/CopyButton'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { toCurl, type CurlSpec } from '@/lib/curl'

interface CurlButtonProps {
  spec: CurlSpec
  title?: string
}

// "Copy as curl" (plan §4.6) — reproduces the exact API call behind a panel.
export function CurlButton({ spec, title = 'API call' }: CurlButtonProps) {
  const command = toCurl(spec)
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Terminal />
          curl
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            The exact request this panel makes — runnable from your terminal. Includes your API
            key.
          </DialogDescription>
        </DialogHeader>
        <div className="relative">
          <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 pr-10 font-mono text-xs leading-relaxed">
            {command}
          </pre>
          <CopyButton value={command} label="Copy command" className="absolute right-2 top-2" />
        </div>
      </DialogContent>
    </Dialog>
  )
}
