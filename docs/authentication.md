# Authentication and tenant authorization

APDL exposes two strict API-key credential kinds. External callers send either
one only in `X-API-Key`:

```text
proj_{project_id}_{secret}     # confidential service credential
client_{project_id}_{token}   # browser-safe public client credential
```

Project IDs are 1-64 alphanumeric characters; secrets and tokens are 16-128
alphanumeric characters. Confidential credentials may carry any canonical
service role. Browser credentials always carry exactly `events:write` and
`config:read`; PostgreSQL rejects a browser credential with any other role.
Consequently a browser credential cannot mutate flags, evaluate trusted
server-side gates, run queries or agents, or reach Codegen. Only Ingestion and
Config recognize the `client_` wire format; every other service requires a
confidential `proj_` credential.

The embedded project ID is a client-side hint, not authority. Services hash the
complete key with SHA-256, look up that hash in PostgreSQL, verify it with a
constant-time comparison, and derive the credential ID, project, roles,
revocation state, and expiry from the stored record. PostgreSQL stores and
constrains the kind and non-secret prefix; Ingestion and Config, the two services
that accept browser keys, also revalidate the wire prefix, stored kind, stored
prefix, project, and browser role ceiling. Any other caller-supplied
`project_id` is an assertion that must equal the verified record's project.

No route accepts credentials from a URL or query parameter. Config streaming
uses the same `X-API-Key` header as `GET /v1/flags`; browser clients must use a
header-capable streaming request rather than native `EventSource`, which cannot
set request headers. This prevents credentials from entering URLs, proxy access
logs, referrers, and browser history.

The Config service exposes `GET /v1/auth/me` for credential introspection. It
returns the verified `credential_id`, `project_id`, and sorted `roles`; it never
echoes the API key. This endpoint is not a human login system.

## Admin user sessions

The Admin Console uses the separate `admin-api` backend-for-frontend. Human
users authenticate with email and password; passwords are stored only as
Argon2id hashes. Project membership and canonical roles live in
`admin_user_projects`.

A successful login creates a random opaque session and CSRF token. PostgreSQL
stores only their SHA-256 digests. The browser receives the session in an
`HttpOnly`, `SameSite=Strict` cookie, so frontend JavaScript cannot read it.
Unsafe requests also require an exact allowed `Origin` and the session-bound
CSRF value. Sessions expire after both an absolute lifetime and an idle window.

The browser calls only `/api/projects/{project_id}/{service}/...`. The Admin API
checks the human user's project and role, strips caller-supplied credentials,
selects the project's configured key or mints a short-lived project key, and
proxies the request. SSE uses the same cookie-authenticated path, so no key
appears in the EventSource URL. Codegen receives the same project-scoped key.
Every authorized mutation is attributed to the human user in
`admin_proxy_audit`; the audit stores route metadata and status, never request
bodies or credentials.

Project authorization does not imply GitHub repository authority. A project
owner, or a member delegated both `agents:manage` and `credentials:manage`, must
complete a project-scoped GitHub App user-authorization flow. The browser never
submits repository or installation coordinates as authority: Codegen uses the
authenticated GitHub user's short-lived token to discover only App-visible
repositories the user administers, persists opaque candidates, and revokes the
token after discovery. The callback relay binds the setup and OAuth legs to the
initiating browser with a short-lived `HttpOnly` cookie, rotates the one-time
state between legs, and requires S256 PKCE. Completing a candidate creates the
grant that binds the APDL project to GitHub's immutable numeric repository ID.
An organization approval request consumes the pending setup state without
starting OAuth and returns a project-scoped approval-required status. Successful
callbacks include Codegen's canonical project ID; Admin validates it against the
signed-in user's workspace list before switching or issuing an authorization
query. Recoverable callback failures clear the correlation cookie and expose
only the fixed `authorization_failed` UI status.

Admin exposes only the grant projection (`grant_id`, `repository_id`, and
display-only `repository_full_name`); the installation ID remains inside the
trusted Codegen control plane. Every GitHub token lease revalidates the active,
same-project grant and uses an operation-specific token restricted to that
immutable repository ID. Repository connection does not grant the separate
project execution authority required for effectful autonomous runs.

### Canonical GitHub App configuration

APDL supports one GitHub.com setup. Configure these seven required values:
`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_BASE64`, `GITHUB_APP_SLUG`,
`GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET`,
`GITHUB_APP_CALLBACK_URL`, and `GITHUB_WEBHOOK_SECRET`. Register the exact
`GITHUB_APP_CALLBACK_URL` as both the App setup URL and OAuth callback URL. Keep
GitHub's automatic **Request user authorization (OAuth) during installation**
option disabled because APDL starts the state-bound OAuth leg itself after the
setup callback. For local development the canonical callback is
`http://localhost:5173/api/github/codegen/callback`. Generate the webhook
signing secret with `openssl rand -hex 32`.

`POST /api/auth/register` accepts one strict `{email, password}` contract. It
creates the user and session in one transaction, but deliberately creates no
`admin_user_projects` rows. A newly registered user is authenticated with
`projects: []` and cannot call any project-scoped service route until an
operator grants membership or the user creates a project from Workspace
settings. Registration requires an exact allowed `Origin` and is rate-limited
with login at the console proxy.

An authenticated user can create a canonical project from
`/settings/workspace`. `POST /api/projects` accepts only `{project_id}`, inserts
the `admin_projects` record and the creator's `admin_user_projects` membership
in one transaction, and returns the refreshed identity. The creator receives
the core ingestion, Config, and Query roles plus read-only Agents access;
`agents:run`, `agents:manage`, and `agents:approve` are not granted. Another
user cannot claim an existing project ID. Database triggers also register
project IDs introduced by operator membership or service-credential
provisioning. Those operator projects have no creator, while a self-created
project permanently retains its creator provenance; deleting the creator is
rejected instead of silently converting the project into an operator project.
The creator also receives `credentials:manage`, a human-membership-only role
that is never accepted by `auth_credentials` and is removed before the Admin
API mints a downstream proxy credential.

## Human-managed SDK credentials

A current project member with `credentials:manage` can create and operate
durable SDK credentials from `/settings/workspace`. Every route re-reads and
locks the live `admin_user_projects` row in its transaction, so removing the
role takes effect without waiting for the human session to expire.

The create request has one strict shape:

```json
{
  "credential_kind": "browser",
  "roles": ["events:write", "config:read"]
}
```

Browser credentials require that exact ordered role pair. Confidential
credentials accept a non-empty canonical-order subset of `events:write`,
`config:read`, `config:evaluate`, and `query:read`. In both cases the roles must
also be present on the human actor's current membership. Managed credentials
cannot carry Config mutation or Agents/Codegen execution roles.

Create and rotate return the plaintext `api_key` once. The Admin Console keeps
it only in the open dialog's component state and clears it when the dialog
closes or unmounts. PostgreSQL receives only its SHA-256 hash. List, revoke, and
audit responses contain metadata only. Operator-provisioned credentials and
five-minute `adminproxy-` credentials have no managed metadata row and are
therefore invisible to these routes.

Rotation creates one active successor with the same project, kind, and roles.
The predecessor deliberately remains active so applications can cut over
without downtime; revoke it explicitly after the new key is deployed. Managed
metadata and create/rotate/revoke audit records are immutable. Audit rows retain
the human UUID, email snapshot, credential kind, roles, and successor link but
never contain a plaintext key or hash. The persistent SDK credential itself
keeps `actor_user_id` null; only short-lived Admin proxy credentials may carry
human command attribution.

## Human login abuse controls

Admin login does not use an email-wide account lock. Before Argon2 verification,
the Admin API atomically consumes short-window global, canonical-network, and
opaque-device budgets. Invalid credentials then increase two independent
per-email progressive-delay records: one for the network and one for the
device. The first two failures remain the generic `401`; the third and later
failures return the strict `auth_throttled` envelope and matching
`Retry-After`.

The Admin edge overwrites `X-Forwarded-For` with its direct peer and clears
other forwarding headers. Uvicorn preserves the socket peer, and the
application accepts one forwarded address only when that peer belongs to the
explicit `APDL_ADMIN_TRUSTED_PROXY_CIDRS` JSON array. Direct peers, malformed
addresses, and forwarded chains cannot select a different risk identity.

The `apdl_admin_device` cookie is a random HttpOnly, SameSite Strict risk
signal; it is not an authentication factor. PostgreSQL receives only
deployment-HMAC digests of the normalized email, client address, and device
token. A correct password from an unthrottled source creates a session even
when the account-wide failure score is high, so knowing an email address is not
enough to deterministically lock out its owner.

Fifty failures for one active account within 24 hours create one unread
`suspicious_login_activity` record. The Admin Console displays the count and
lets the user acknowledge it. This durable notification preserves the risk
signal without turning it into an authorization decision.

For projects without an operator-configured key in `APDL_SERVICE_API_KEYS`, the
Admin API mints a random five-minute credential for each proxied request. Only
the SHA-256 hash is stored in `auth_credentials`; the raw key remains in memory
for the upstream call and the row is deleted after the response or SSE stream
closes. This keeps self-created projects usable without exposing a persistent
service credential to the browser or storing a recoverable key.

Effectful Agents and Codegen execution requires a canonical
`admin_project_execution_authorizations` row. Migration 028 backfills and
automatically records `operator_provisioned` authority only for projects whose
immutable `admin_projects.created_by` is null. Self-created projects retain
`agents:read` by default. An operator may authorize one deliberately with the
`self_registered_override` source, a non-empty actor, reason, and timestamp.
Agents and Codegen independently load that record, while PostgreSQL rejects
`agents:approve` and inserts into registered effect-bearing tables when it is
absent. Governed analysis uses `agents:run`/`agents:manage`, requires active
owner-controlled Agents setup, and cannot mint downstream mutation authority.

Experiment analysis uses synchronous authority delegation. After Query has
authenticated either a confidential `X-API-Key` or a Query-and-Config internal
capability and enforced `query:read`, it forwards that exact header to Config's
read-only analysis projection. Config independently reauthenticates it and
derives the tenant only from the verified record. Query never accepts or
selects a second Config key. For an Admin request, the Admin API keeps its
ephemeral proxy credential alive until the nested Query-to-Config request and
outer response both complete.

`make create-admin-user` remains the operator-only bootstrap and recovery path.
Reprovisioning an existing email rotates its password and revokes active
sessions. An execution role on a previously unauthorized self-created project
requires all three explicit options:

```bash
make create-admin-user ARGS="\
  --email operator@example.com \
  --project-id acme \
  --roles agents:approve \
  --allow-self-registered-execution \
  --override-actor operator@example.com \
  --override-reason 'Approved production automation boundary'"
```

The authorization and role grant commit together. The authorization record is
immutable audit evidence; operational access is stopped by removing execution
roles and revoking credentials.

## Roles

| Role | Authority |
|------|-----------|
| `events:write` | Publish events for the credential project |
| `config:read` | Read client-visible flags and the configuration stream |
| `config:write` | Read and mutate administrative flag/experiment state |
| `config:evaluate` | Perform trusted server-side flag evaluation |
| `query:read` | Run analytics queries |
| `agents:read` | Read agent definitions, runs, results, and audit entries |
| `agents:run` | Trigger agent runs |
| `agents:manage` | Create, test, update, and archive custom agents |
| `agents:approve` | Approve or reject gated agent actions |
| `credentials:manage` | Human-only creation, rotation, revocation, and audit of restricted SDK credentials |

Self-created projects begin with `agents:read`. Owner activation of governed
setup may add `agents:run` and `agents:manage` for L1/L2 analysis.
`agents:approve`, Codegen, and external effects remain valid only for
operator-provisioned or explicitly authorized self-created projects.

## Agents execution capabilities

`admin_projects` is the durable project identity, and
`admin_project_execution_authorizations` records whether that project may run
effectful Agents or Codegen work. APDL does not create another project secret or
capability when a project is initialized. Such a credential would become
ambient project authority and could outlive the run that needed it.

Instead, a live Agents worker connects to PostgreSQL as the dedicated
non-owner `apdl_agents` identity. Immediately before one leased `agent_run`,
`custom_agent_test`, or `approval_effect` makes an internal HTTP call, Agents
mints a random 60-second token and inserts only its SHA-256 hash into
`agent_service_capabilities`. The row binds the authority to the exact project,
execution and run IDs, lease owner, target audiences, canonical roles, and
expiry. A mutation row additionally stores a SHA-256 binding over the uppercase
method, exact path, canonical JSON body, and hashed `Idempotency-Key`. The raw
token exists only in process memory and the
`X-APDL-Internal-Capability` request header; Agents deletes the row when the
call ends, with expiry as the cleanup fallback.

Config, Query, and Codegen independently hash the presented token, require
their audience and the route's role, and revalidate the referenced durable
execution and live lease. Codegen also rechecks project execution
authorization. The `config:write` and `agents:manage` roles can be issued only
for a leased approval effect; ordinary runs and tests receive read or
server-side evaluation authority. Config rejects internal
capabilities on its long-lived SSE stream, and every service rejects requests
that combine `X-API-Key` with `X-APDL-Internal-Capability`.

Config and Codegen recompute the mutation request binding, revalidate the live
approval-effect lease, and atomically set `consumed_at` through one narrowly
owned `SECURITY DEFINER` routine. A mismatch, replay, expired token, or
future-issued token fails closed. Read/query capabilities remain reusable only
within their short lifetime so Query can delegate the same verified authority
to Config's read-only experiment projection.

The capability therefore serves both purposes without conflating them: its
stored project binding identifies the tenant for the one call, while its
audience, roles, and live execution binding prove the limited authority being
delegated. It is not a reusable project API key. The ordinary `apdl_runtime`
database identity may read capability rows to validate them but cannot insert,
update, or delete them. Only the Agents issuer identity has exact-column insert
and delete authority. No login role can update a capability; the non-login
consume-function owner can update only `consumed_at` through the atomic routine.

## Operator provision credentials

Apply the PostgreSQL migrations first with `make migrate-postgres`. Generate a
key, hash the full key, and insert only the hash. This direct SQL path is for
operator-owned infrastructure credentials; normal project members should use
the audited Admin Console workflow above. A confidential service credential
declares its kind and non-secret prefix explicitly:

```bash
api_key="proj_acme_$(openssl rand -hex 24)"
key_hash="$(printf %s "$api_key" | shasum -a 256 | awk '{print $1}')"
credential_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"

psql "$POSTGRES_URL" \
  -v credential_id="$credential_id" \
  -v project_id="acme" \
  -v key_hash="$key_hash" <<'SQL'
INSERT INTO auth_credentials (
  credential_id, project_id, credential_kind, key_prefix, key_hash, roles
)
VALUES (
  :'credential_id',
  :'project_id',
  'confidential',
  'proj_acme_',
  :'key_hash',
  ARRAY['config:write', 'config:evaluate']
);
SQL

printf 'API key (shown once): %s\n' "$api_key"
```

A browser key uses the `client_` prefix and the exact browser role set:

```bash
client_key="client_acme_$(openssl rand -hex 24)"
key_hash="$(printf %s "$client_key" | shasum -a 256 | awk '{print $1}')"
credential_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"

psql "$POSTGRES_URL" \
  -v credential_id="$credential_id" \
  -v key_hash="$key_hash" <<'SQL'
INSERT INTO auth_credentials (
  credential_id, project_id, credential_kind, key_prefix, key_hash, roles
)
VALUES (
  :'credential_id',
  'acme',
  'browser',
  'client_acme_',
  :'key_hash',
  ARRAY['events:write', 'config:read']
);
SQL

printf 'Browser key (shown once): %s\n' "$client_key"
```

Service principals receive only the roles they need. `APDL_SERVICE_API_KEYS`
is an optional Admin API-only map for deployments that choose persistent
project-scoped proxy credentials instead of the Admin API's per-request
five-minute credentials:

```text
APDL_SERVICE_API_KEYS={"acme":"proj_acme_<secret>"}
```

Agents does not read this map. Its Config, Query, and Codegen calls use the
execution capabilities described above. Automatic guardrail mutation remains
disabled in the OSS developer preview.

## Rotation, revocation, and expiry

- Managed credentials rotate through the Admin Console/API. The successor is
  revealed once; move clients to it, then explicitly revoke the predecessor.
- Operator credentials rotate by inserting a second active credential for the
  same project, moving clients to it, then revoking the old record.
- Revoke immediately with
  `UPDATE auth_credentials SET active = FALSE, revoked_at = NOW() WHERE credential_id = ...`.
- Set `expires_at` for short-lived credentials. Expired records are rejected.
- Never store the plaintext key in PostgreSQL or logs.

Normal local bootstrap runs only the PostgreSQL schema migrations, so the
project and credential catalogs start empty. Register through the loopback
Admin Console, create a project in Project management, and create reveal-once
browser or confidential credentials there. The isolated fresh-smoke suite owns
separate `APDL_SMOKE_CONFIDENTIAL_KEY` and `APDL_SMOKE_BROWSER_KEY` fixtures; it
first verifies the catalogs are empty, provisions those fixtures with the
test-only SQL under `scripts/fixtures/`, and destroys the isolated volumes when
the suite finishes. Production deployments should set
`APDL_SERVICE_API_KEYS` only on the Admin API and only to confidential project
keys when persistent proxy credentials are desired. They should also assign a
unique `APDL_AGENTS_POSTGRES_PASSWORD`, set
`APDL_ADMIN_COOKIE_SECURE=true`, configure an exact HTTPS origin, disable public
registration, and provision least-privilege credentials through their normal
secret-management workflow.
