# Authentication and tenant authorization

APDL uses one canonical API-key contract across ingestion, config, query,
agents, and codegen. Send the credential in `X-API-Key`:

```text
proj_{project_id}_{secret}
```

Project IDs are 1-64 alphanumeric characters and secrets are 16-128
alphanumeric characters.

The embedded project ID is a client-side hint, not authority. Each service
hashes the complete key with SHA-256, looks up that hash in PostgreSQL, verifies
it with a constant-time comparison, and derives the credential ID, project,
roles, revocation state, and expiry from the stored record. Authentication also
rejects a key whose embedded project differs from its stored project, preventing
misprovisioned keys from splitting client and server tenant state. Any other
caller-supplied `project_id` is an assertion that must equal the verified
record's project.

`GET /v1/stream` temporarily also accepts `api_key` in the query string for the
existing EventSource clients. No other route accepts query-string credentials.

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
the canonical project roles; another user cannot claim an existing project ID.
Database triggers also register project IDs introduced by operator membership
or service-credential provisioning, and foreign keys keep both registries tied
to the canonical project row.

For projects without an operator-configured key in `APDL_SERVICE_API_KEYS`, the
Admin API mints a random five-minute credential for each proxied request. Only
the SHA-256 hash is stored in `auth_credentials`; the raw key remains in memory
for the upstream call and the row is deleted after the response or SSE stream
closes. This keeps self-created projects usable without exposing a persistent
service credential to the browser or storing a recoverable key.

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

## Provision a credential

Apply the PostgreSQL migrations first with `make migrate-postgres`. Generate a
key, hash the full key, and insert only the hash:

```bash
api_key="proj_acme_$(openssl rand -hex 24)"
key_hash="$(printf %s "$api_key" | shasum -a 256 | awk '{print $1}')"
credential_id="$(uuidgen | tr '[:upper:]' '[:lower:]')"

psql "$POSTGRES_URL" \
  -v credential_id="$credential_id" \
  -v project_id="acme" \
  -v key_hash="$key_hash" <<'SQL'
INSERT INTO auth_credentials (credential_id, project_id, key_hash, roles)
VALUES (
  :'credential_id',
  :'project_id',
  :'key_hash',
  ARRAY['events:write', 'config:read']
);
SQL

printf 'API key (shown once): %s\n' "$api_key"
```

Service principals receive only the roles they need. In production, the Admin
API, internal agents, and the query guardrail monitor read one JSON object from
`APDL_SERVICE_API_KEYS`, keyed by project, so each automated call uses a
tenant-scoped credential. Agent-to-Codegen calls use that same project scope:

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

`APDL_DEV_API_KEY` is the only local-development credential setting. When set,
`scripts/init-postgres.sh` derives its project from the key, provisions
its hash with all roles, and the smoke test and internal services reuse it.
Production deployments must leave it unset, set `APDL_SERVICE_API_KEYS` for
internal calls, set `APDL_ADMIN_COOKIE_SECURE=true`, configure an exact HTTPS
origin, and provision least-privilege credentials through their normal
secret-management workflow.
