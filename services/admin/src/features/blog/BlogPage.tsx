import { ArrowRight, Clock3, Code2, Server } from 'lucide-react'
import { Link } from 'react-router-dom'

import { PageHeader } from '@/components/shared/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const ARTICLES = [
  {
    to: '/blog/javascript-sdk',
    title: 'How to set up the JavaScript SDK',
    description:
      'Connect a browser or React application, capture your first event, and evaluate a feature flag locally.',
    install: 'npm install @apdl-oss/sdk',
    readingTime: '6 min read',
    icon: Code2,
    accentClassName: 'bg-gradient-to-r from-amber-500 to-orange-500',
  },
  {
    to: '/blog/python-sdk',
    title: 'How to set up the Python SDK',
    description:
      'Connect a backend service with a confidential credential, background delivery, and server-side flag evaluation.',
    install: 'uv add apdl-sdk',
    readingTime: '7 min read',
    icon: Server,
    accentClassName: 'bg-gradient-to-r from-sky-500 to-indigo-500',
  },
] as const

export function BlogPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Blog"
        description="Practical guides for connecting your product to APDL."
      />

      <div className="grid gap-5 lg:grid-cols-2">
        {ARTICLES.map((article) => {
          const Icon = article.icon
          return (
            <Link key={article.to} to={article.to} className="group block h-full">
              <Card className="h-full overflow-hidden transition-all group-hover:-translate-y-0.5 group-hover:border-primary/35 group-hover:shadow-md">
                <div className={cn('h-1.5', article.accentClassName)} />
                <CardHeader className="space-y-4">
                  <div className="flex items-start justify-between gap-4">
                    <div
                      className={cn(
                        'flex h-11 w-11 items-center justify-center rounded-lg text-white shadow-sm',
                        article.accentClassName,
                      )}
                    >
                      <Icon className="h-5 w-5" />
                    </div>
                    <Badge variant="secondary">SDK setup</Badge>
                  </div>
                  <div className="space-y-2">
                    <CardTitle className="text-lg">{article.title}</CardTitle>
                    <CardDescription className="min-h-10 leading-5">
                      {article.description}
                    </CardDescription>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <div className="rounded-md border bg-zinc-950 px-3 py-2.5 font-mono text-xs text-zinc-200">
                    <span className="mr-2 text-zinc-500">$</span>
                    {article.install}
                  </div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="inline-flex items-center gap-1.5 text-muted-foreground">
                      <Clock3 className="h-3.5 w-3.5" />
                      {article.readingTime}
                    </span>
                    <span className="inline-flex items-center gap-1 font-medium text-foreground">
                      Read guide
                      <ArrowRight className="h-3.5 w-3.5 transition-transform group-hover:translate-x-0.5" />
                    </span>
                  </div>
                </CardContent>
              </Card>
            </Link>
          )
        })}
      </div>

      <div className="rounded-lg border border-dashed bg-muted/30 p-5">
        <p className="font-medium">Start with a credential</p>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Create a browser or server credential in{' '}
          <Link to="/settings/workspace" className="font-medium text-foreground underline underline-offset-4">
            Project management
          </Link>
          , then follow the matching guide above.
        </p>
      </div>
    </div>
  )
}
