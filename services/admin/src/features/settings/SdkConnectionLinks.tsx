import { ArrowRight, Code2, Server } from 'lucide-react'
import { Link } from 'react-router-dom'

const GUIDES = [
  {
    to: '/blog/javascript-sdk',
    title: 'Connect the JavaScript SDK',
    description: 'Browser, React, and Next.js setup',
    icon: Code2,
  },
  {
    to: '/blog/python-sdk',
    title: 'Connect the Python SDK',
    description: 'Backend and server-side setup',
    icon: Server,
  },
] as const

export function SdkConnectionLinks() {
  return (
    <section aria-labelledby="sdk-connection-guides" className="rounded-lg border bg-muted/30 p-4">
      <div className="mb-3">
        <h3 id="sdk-connection-guides" className="text-sm font-medium">
          Connect an SDK
        </h3>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Follow a step-by-step guide after creating your credential.
        </p>
      </div>
      <div className="grid gap-2 md:grid-cols-2">
        {GUIDES.map((guide) => {
          const Icon = guide.icon
          return (
            <Link
              key={guide.to}
              to={guide.to}
              className="group flex items-center gap-3 rounded-md border bg-card p-3 transition-colors hover:border-primary/30 hover:bg-accent/50"
            >
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <Icon className="h-4 w-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium">{guide.title}</span>
                <span className="block text-xs text-muted-foreground">{guide.description}</span>
              </span>
              <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:text-foreground" />
            </Link>
          )
        })}
      </div>
    </section>
  )
}
