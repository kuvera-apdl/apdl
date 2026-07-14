# Authentication and tenant authorization

APDL has two strict credential kinds. Send either one only in `X-API-Key`:

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

Project authorization does not imply GitHub repository ownership. Codegen
accepts no tenant-supplied repository or GitHub App installation coordinates.
A trusted operator must separately activate a grant binding the APDL project to
GitHub's immutable numeric repository ID. Admin exposes only the read-only grant
projection (`grant_id`, `repository_id`, and display-only
`repository_full_name`); the installation ID remains inside the trusted
Codegen control plane. Every GitHub token lease revalidates that grant and uses
an operation-specific token restricted to the immutable repository ID.

`POST /api/auth/register` accepts one strict `{email, password}` contract. It
creates the user and session in one transaction, but deliberately creates no
`admin_user_projects` rows. A newly registered user is authenticated with
`projects: []` and cannot call any project-scoped service route until an
operator grants membership and roles separately. Registration requires an
exact allowed `Origin` and is rate-limited with login at the console proxy.

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

For projects without an operator-configured key in `APDL_SERVICE_API_KEYS`, the
Admin API mints a random five-minute credential for each proxied request. Only
the SHA-256 hash is stored in `auth_credentials`; the raw key remains in memory
for the upstream call and the row is deleted after the response or SSE stream
closes. This keeps self-created projects usable without exposing a persistent
service credential to the browser or storing a recoverable key.

Agents execution is available only to operator-provisioned projects whose
canonical `admin_projects.created_by` is null. Self-created projects retain
`agents:read`, but the Admin proxy will not mint an execution credential for
them and the Agents service independently rejects execution roles even if a
long-lived credential was manually overprivileged. PostgreSQL also rejects new
`agent_runs` rows for self-created or unknown projects.

Experiment analysis uses synchronous credential delegation: after Query has
authenticated a confidential `X-API-Key` and enforced `query:read` for its
project, it forwards that same header to Config's read-only analysis
projection. Config independently reauthenticates the credential and derives
the tenant only from it. Query never accepts or selects a second Config key;
the Admin API keeps an ephemeral proxy credential alive until the nested
Query-to-Config request and outer response both complete.

`make create-admin-user` remains the operator-only bootstrap and recovery path.
Reprovisioning an existing email rotates its password and revokes active
sessions.

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

The three Agents execution roles are valid only for operator-provisioned
projects. Self-created projects are restricted to `agents:read` at membership,
credential, service-authorization, and run-storage boundaries.

## Provision credentials

Apply the PostgreSQL migrations first with `make migrate-postgres`. Generate a
key, hash the full key, and insert only the hash. A confidential service
credential declares its kind and non-secret prefix explicitly:

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

Service principals receive only the roles they need. Internal agents read one
JSON object from `APDL_SERVICE_API_KEYS`, keyed by project, so each automated
call uses a tenant-scoped credential. Agent-to-Codegen calls use that same
project scope. Automatic guardrail mutation is disabled in the OSS developer
preview:

```text
APDL_SERVICE_API_KEYS={"acme":"proj_acme_<secret>"}
```

## Rotation, revocation, and expiry

- Rotate by inserting a second active credential for the same project, moving
  clients to it, then revoking the old record.
- Revoke immediately with
  `UPDATE auth_credentials SET active = FALSE, revoked_at = NOW() WHERE credential_id = ...`.
- Set `expires_at` for short-lived credentials. Expired records are rejected.
- Never store the plaintext key in PostgreSQL or logs.

For local development, `APDL_DEV_API_KEY` provisions one confidential core key
with `events:write`, Config read/write/evaluate, and `query:read`. It carries no
Agents roles.
`APDL_DEV_CLIENT_KEY` provisions one browser key with exactly `events:write`
and `config:read`. `scripts/init-postgres.sh` derives and stores each kind,
project, non-secret prefix, and hash. Production deployments must leave both
settings unset, set `APDL_SERVICE_API_KEYS` only to confidential project keys
for internal calls, set `APDL_ADMIN_COOKIE_SECURE=true`, configure an exact
HTTPS origin, and provision least-privilege credentials through their normal
secret-management workflow.
