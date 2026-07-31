import { Server } from 'lucide-react'
import { Link } from 'react-router-dom'

import {
  ArticleSection,
  Checklist,
  CodeBlock,
  GuideCallout,
  SdkArticleLayout,
} from '@/features/blog/SdkArticleLayout'

const SECTIONS = [
  { id: 'create-credential', label: 'Create a server credential' },
  { id: 'install', label: 'Install the package' },
  { id: 'configure', label: 'Configure secrets' },
  { id: 'initialize', label: 'Initialize the client' },
  { id: 'feature-flags', label: 'Evaluate a feature flag' },
  { id: 'verify', label: 'Verify and ship' },
]

const INSTALL_UV = 'uv add apdl-sdk'
const INSTALL_PIP = 'pip install apdl-sdk'

const ENVIRONMENT = `APDL_URL=https://apdl.example.com
APDL_API_KEY=proj_yourproject_replace_me`

const QUICK_START = `import os

from apdl import APDL

with APDL.init(
    api_key=os.environ["APDL_API_KEY"],
    endpoint=os.environ["APDL_URL"],
) as client:
    client.track(
        "order_completed",
        {"total": 42.0, "currency": "CAD"},
        user_id="u_123",
    )
    client.identify("u_123", {"plan": "pro"})

    variant = client.get_variant(
        "new-checkout",
        user_id="u_123",
    )
    if variant == "treatment":
        enable_new_checkout()`

const LONG_LIVED = `client = APDL.init(
    api_key=os.environ["APDL_API_KEY"],
    endpoint=os.environ["APDL_URL"],
)

try:
    run_application(client)
finally:
    report = client.shutdown()
    if not report.complete:
        persist_for_replay(report.undelivered_events)`

const FLAG_DETAILS = `result = client.get_variant_details(
    "new-checkout",
    user_id="u_123",
    attributes={"plan": "pro", "region": "ca"},
)

print(result.variant, result.reason, result.rule_id)`

export function PythonSdkArticlePage() {
  return (
    <SdkArticleLayout
      title="How to set up the Python SDK"
      description="Connect a backend service to APDL with a confidential credential, non-blocking event delivery, and local feature-flag evaluation."
      icon={Server}
      accentClassName="bg-gradient-to-r from-sky-500 to-indigo-500"
      readingTime="7 min read"
      sections={SECTIONS}
    >
      <ArticleSection id="create-credential" title="1. Create a server credential">
        <p>
          Open{' '}
          <Link
            to="/settings/workspace"
            className="font-medium text-foreground underline underline-offset-4"
          >
            Project management
          </Link>{' '}
          and find <strong className="text-foreground">SDK credentials</strong>. Choose{' '}
          <strong className="text-foreground">Create credential</strong>, select{' '}
          <strong className="text-foreground">Server SDK</strong>, and grant only the roles the
          service needs.
        </p>
        <p>
          For event capture plus local flag evaluation, select{' '}
          <code className="font-mono text-foreground">events:write</code> and{' '}
          <code className="font-mono text-foreground">config:read</code>. Save the key while the
          reveal dialog is open.
        </p>
        <GuideCallout title="Keep this key server-side" tone="warning">
          Python accepts only a confidential{' '}
          <code className="font-mono text-foreground">proj_…</code> credential. Store it in your
          secret manager and never commit it, log it, or expose it to a browser.
        </GuideCallout>
      </ArticleSection>

      <ArticleSection id="install" title="2. Install the package">
        <p>APDL requires Python 3.12 or newer. Install it with your project package manager.</p>
        <CodeBlock code={INSTALL_UV} label="uv" language="shell" />
        <p>Or install it directly with pip:</p>
        <CodeBlock code={INSTALL_PIP} label="pip" language="shell" />
      </ArticleSection>

      <ArticleSection id="configure" title="3. Configure secrets">
        <p>
          Add the gateway origin and reveal-once key to your deployment secret store. The Python
          SDK does not read environment variables automatically, so your application passes them
          during initialization.
        </p>
        <CodeBlock code={ENVIRONMENT} label="Environment" language="dotenv" />
        <p>
          The endpoint is required and must be an HTTP(S) origin without a path, query string,
          credentials, or fragment.
        </p>
      </ArticleSection>

      <ArticleSection id="initialize" title="4. Initialize the client">
        <p>
          A context manager is the simplest lifecycle: it starts background delivery on entry and
          drains the queue on exit. Identity stays explicit on each call because one server process
          handles many users.
        </p>
        <CodeBlock code={QUICK_START} label="Quick start" language="python" />
        <GuideCallout title="Long-lived services">
          Create one client for the service process and always inspect the shutdown report. If a
          final delivery cannot complete, persist the undelivered events before the process exits.
        </GuideCallout>
        <CodeBlock code={LONG_LIVED} label="Application lifecycle" language="python" />
      </ArticleSection>

      <ArticleSection id="feature-flags" title="5. Evaluate a feature flag">
        <p>
          The SDK refreshes flag configuration in the background and evaluates variants locally.
          Pass the same stable user ID and targeting attributes your product uses elsewhere.
        </p>
        <CodeBlock code={FLAG_DETAILS} label="Evaluation details" language="python" />
        <p>
          A single-flag evaluation logs a deduplicated exposure automatically. Bulk snapshot methods
          do not log exposures, so evaluate the specific flag where the user actually sees the
          experience.
        </p>
      </ArticleSection>

      <ArticleSection id="verify" title="6. Verify and ship">
        <p>
          Run a request that tracks a test event, then open{' '}
          <Link
            to="/settings/verify"
            className="font-medium text-foreground underline underline-offset-4"
          >
            Verify integration
          </Link>{' '}
          to confirm that the event reached APDL.
        </p>
        <Checklist
          items={[
            'The key begins with proj_',
            'The key lives in a secret manager',
            'The endpoint is a gateway origin only',
            'Every event has an explicit user or anonymous ID',
            'Shutdown reports are handled on process exit',
            'The verification event appears in APDL',
          ]}
        />
      </ArticleSection>
    </SdkArticleLayout>
  )
}
