import { ArrowLeft, CheckCircle2, Clock3, type LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { CopyButton } from '@/components/shared/CopyButton'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

interface ArticleSectionLink {
  id: string
  label: string
}

interface SdkArticleLayoutProps {
  title: string
  description: string
  icon: LucideIcon
  accentClassName: string
  readingTime: string
  sections: ArticleSectionLink[]
  children: ReactNode
}

export function SdkArticleLayout({
  title,
  description,
  icon: Icon,
  accentClassName,
  readingTime,
  sections,
  children,
}: SdkArticleLayoutProps) {
  return (
    <div className="space-y-6">
      <Link
        to="/blog"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to blog
      </Link>

      <header className="overflow-hidden rounded-xl border bg-card">
        <div className={cn('h-1.5 w-full', accentClassName)} />
        <div className="grid gap-6 p-6 md:grid-cols-[1fr_auto] md:items-start md:p-8">
          <div className="max-w-3xl space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">SDK setup</Badge>
              <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                <Clock3 className="h-3.5 w-3.5" />
                {readingTime}
              </span>
            </div>
            <div className="space-y-2">
              <h1 className="text-2xl font-semibold tracking-tight md:text-3xl">{title}</h1>
              <p className="max-w-2xl text-sm leading-6 text-muted-foreground md:text-base">
                {description}
              </p>
            </div>
          </div>
          <div
            className={cn(
              'flex h-14 w-14 items-center justify-center rounded-xl text-white shadow-sm',
              accentClassName,
            )}
            aria-hidden="true"
          >
            <Icon className="h-7 w-7" />
          </div>
        </div>
      </header>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_16rem]">
        <article className="min-w-0 space-y-8">{children}</article>

        <Card className="hidden lg:sticky lg:top-6 lg:block">
          <CardContent className="p-4">
            <p className="mb-3 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              In this guide
            </p>
            <nav aria-label="Article sections">
              <ol className="space-y-1">
                {sections.map((section, index) => (
                  <li key={section.id}>
                    <a
                      href={`#${section.id}`}
                      className="flex gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
                    >
                      <span className="font-mono text-xs">{String(index + 1).padStart(2, '0')}</span>
                      <span>{section.label}</span>
                    </a>
                  </li>
                ))}
              </ol>
            </nav>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}

export function ArticleSection({
  id,
  title,
  children,
}: {
  id: string
  title: string
  children: ReactNode
}) {
  return (
    <section id={id} className="scroll-mt-6 space-y-4">
      <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
      <div className="space-y-4 text-sm leading-7 text-muted-foreground">{children}</div>
    </section>
  )
}

export function CodeBlock({
  code,
  label,
  language,
}: {
  code: string
  label: string
  language: string
}) {
  return (
    <div className="overflow-hidden rounded-lg border bg-zinc-950 text-zinc-100 shadow-sm">
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-2">
        <div className="flex items-center gap-2 text-xs text-zinc-400">
          <span>{label}</span>
          <span aria-hidden="true">·</span>
          <span className="font-mono">{language}</span>
        </div>
        <CopyButton
          value={code}
          label={`Copy ${label}`}
          className="text-zinc-400 hover:bg-white/10 hover:text-white"
        />
      </div>
      <pre className="overflow-x-auto p-4 text-[13px] leading-6">
        <code>{code}</code>
      </pre>
    </div>
  )
}

export function GuideCallout({
  title,
  children,
  tone = 'default',
}: {
  title: string
  children: ReactNode
  tone?: 'default' | 'warning'
}) {
  return (
    <div
      className={cn(
        'rounded-lg border p-4',
        tone === 'warning'
          ? 'border-amber-500/30 bg-amber-500/10'
          : 'border-primary/15 bg-muted/50',
      )}
    >
      <p className="font-medium text-foreground">{title}</p>
      <div className="mt-1 text-sm leading-6 text-muted-foreground">{children}</div>
    </div>
  )
}

export function Checklist({ items }: { items: string[] }) {
  return (
    <ul className="grid gap-2 sm:grid-cols-2">
      {items.map((item) => (
        <li key={item} className="flex items-start gap-2 rounded-md border bg-card p-3 text-sm">
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
          <span className="leading-5 text-foreground">{item}</span>
        </li>
      ))}
    </ul>
  )
}
