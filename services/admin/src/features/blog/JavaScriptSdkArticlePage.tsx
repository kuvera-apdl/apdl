import { Code2 } from 'lucide-react'
import { Link } from 'react-router-dom'

import {
  ArticleSection,
  Checklist,
  CodeBlock,
  GuideCallout,
  SdkArticleLayout,
} from '@/features/blog/SdkArticleLayout'

const SECTIONS = [
  { id: 'create-credential', label: 'Create a browser credential' },
  { id: 'install', label: 'Install the package' },
  { id: 'initialize', label: 'Initialize the SDK' },
  { id: 'send-events', label: 'Send your first events' },
  { id: 'feature-flags', label: 'Evaluate a feature flag' },
  { id: 'verify', label: 'Verify the connection' },
]

const INSTALL = 'npm install @apdl-oss/sdk'

const INITIALIZE = `import { APDL } from '@apdl-oss/sdk'

const apdl = APDL.init({
  endpoint: 'https://apdl.example.com',
  auth: {
    clientKey: 'client_yourproject_replace_me',
  },
  autoCapture: true,
  privacyMode: 'standard',
  consent: {
    analytics: false,
    personalization: false,
    experiments: false,
  },
})

// Update APDL when your consent manager records the user's choice.
apdl.consent.update({
  analytics: true,
  experiments: true,
})`

const TRACK_EVENTS = `apdl.track('checkout_started', {
  cart_value: 89.50,
  item_count: 3,
})

apdl.identify('user-42', {
  plan: 'pro',
  region: 'ca',
})`

const FEATURE_FLAG = `const variant = apdl.getVariant('new-checkout-flow')

if (variant === 'treatment') {
  renderNewCheckout()
} else {
  renderCurrentCheckout()
}`

const NEXT_ENV = `NEXT_PUBLIC_APDL_URL=https://apdl.example.com
NEXT_PUBLIC_APDL_CLIENT_KEY=client_yourproject_replace_me`

const NEXT_PROVIDER = `import { APDLProvider } from '@apdl-oss/sdk/react'

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <APDLProvider autoCapture>{children}</APDLProvider>
}`

export function JavaScriptSdkArticlePage() {
  return (
    <SdkArticleLayout
      title="How to set up the JavaScript SDK"
      description="Connect a browser application to APDL, capture product events, and evaluate feature flags without a server round-trip."
      icon={Code2}
      accentClassName="bg-gradient-to-r from-amber-500 to-orange-500"
      readingTime="6 min read"
      sections={SECTIONS}
    >
      <ArticleSection id="create-credential" title="1. Create a browser credential">
        <p>
          Open{' '}
          <Link
            to="/settings/workspace"
            className="font-medium text-foreground underline underline-offset-4"
          >
            Workspace settings
          </Link>{' '}
          and find <strong className="text-foreground">SDK credentials</strong>. Choose{' '}
          <strong className="text-foreground">Create credential</strong>, keep the type set to{' '}
          <strong className="text-foreground">Browser SDK</strong>, and save the key while the
          reveal dialog is open.
        </p>
        <GuideCallout title="Use the browser-safe key">
          JavaScript uses the canonical <code className="font-mono text-foreground">client_…</code>{' '}
          credential. It is restricted to event writes and client-visible configuration reads.
          Never put a confidential <code className="font-mono text-foreground">proj_…</code> key
          in browser code.
        </GuideCallout>
      </ArticleSection>

      <ArticleSection id="install" title="2. Install the package">
        <p>Install the APDL package in the application that will capture product activity.</p>
        <CodeBlock code={INSTALL} label="Install" language="shell" />
        <p>
          Your endpoint must be the absolute HTTP(S) origin of the APDL gateway, with no path,
          query string, credentials, or fragment.
        </p>
      </ArticleSection>

      <ArticleSection id="initialize" title="3. Initialize the SDK">
        <p>
          Initialize once near your application entry point. APDL starts fail-closed: analytics,
          personalization, and experiment activity stay off until your consent manager supplies the
          current user choice.
        </p>
        <CodeBlock code={INITIALIZE} label="apdl.ts" language="typescript" />
        <GuideCallout title="React and Next.js">
          The first-party React adapter owns the client boundary and singleton lifecycle. Add these
          environment variables, then mount the provider once in your root layout.
        </GuideCallout>
        <CodeBlock code={NEXT_ENV} label=".env.local" language="dotenv" />
        <CodeBlock code={NEXT_PROVIDER} label="app/layout.tsx" language="tsx" />
      </ArticleSection>

      <ArticleSection id="send-events" title="4. Send your first events">
        <p>
          Use clear, stable event names and pass only the properties your analytics questions need.
          Call <code className="font-mono text-foreground">identify</code> after login so later
          events and flag evaluations use the product user ID.
        </p>
        <CodeBlock code={TRACK_EVENTS} label="Track and identify" language="typescript" />
      </ArticleSection>

      <ArticleSection id="feature-flags" title="5. Evaluate a feature flag">
        <p>
          APDL keeps flag configuration up to date and evaluates the assigned variant locally. A
          real evaluation automatically records a deduplicated feature-flag exposure.
        </p>
        <CodeBlock code={FEATURE_FLAG} label="Local evaluation" language="typescript" />
      </ArticleSection>

      <ArticleSection id="verify" title="6. Verify the connection">
        <p>
          Trigger a test event in your application, then open{' '}
          <Link
            to="/settings/verify"
            className="font-medium text-foreground underline underline-offset-4"
          >
            Verify integration
          </Link>{' '}
          to confirm that APDL received it.
        </p>
        <Checklist
          items={[
            'The key begins with client_',
            'The endpoint is a gateway origin only',
            'Consent mirrors the current user choice',
            'Event names stay stable across releases',
            'No confidential key is shipped to the browser',
            'The verification event appears in APDL',
          ]}
        />
      </ArticleSection>
    </SdkArticleLayout>
  )
}
