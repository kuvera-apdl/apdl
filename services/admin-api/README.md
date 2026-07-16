# APDL Admin API

Backend-for-frontend for the APDL Admin Console. It is the browser security
boundary: human credentials terminate here, service credentials remain
server-side except during an explicit reveal-once creation or rotation, and
every request is authorized against a user, project, and role.

## Security model

- Administrator passwords use Argon2id and are never returned by an API.
- Login creates a random opaque session. PostgreSQL stores only its SHA-256
  digest; the browser receives the raw value in an `HttpOnly`,
  `SameSite=Strict` cookie.
- Sessions have absolute and idle expiry. Logout and password reprovisioning
  revoke server-side sessions.
- Long-lived SSE connections independently re-check the session, current project
  membership, and exact required role every five seconds. Session loss emits
  `auth_expired`; project or role loss emits `project_access_revoked`. Registry
  failures close the stream fail-closed.
- Five consecutive failures lock an account for 15 minutes by default. Login
  failures use one generic response to avoid user enumeration.
- Registration accepts only an email and password. New accounts start with no
  rows in `admin_user_projects`, so registration cannot grant tenant access or
  service roles.
- Authenticated users can create a project through `POST /api/projects`. The
  project record and creator membership are committed together, and the
  updated project list is returned to the console. Creator membership includes
  core analytics, `agents:read`, and the human-only `credentials:manage` role,
  but excludes Agents run, management, and approval authority.
- `credentials:manage` is valid only on human memberships and is filtered from
  every short-lived proxy credential. Its routes re-read and lock the current
  database membership instead of trusting roles captured when the session was
  established.
- Managed browser credentials carry exactly `events:write` and `config:read`.
  Managed confidential credentials may carry only an explicit, canonical
  subset of `events:write`, `config:read`, `config:evaluate`, and `query:read`,
  and never exceed the creating member's current roles.
- PostgreSQL stores only the managed key hash. Create and rotate responses
  reveal the raw key once; list, revoke, and audit responses never include it.
  Rotation creates one active successor with the same kind and roles and
  leaves the predecessor active for an explicit cutover and revocation.
- Only credentials present in `admin_managed_credentials` are visible through
  these APIs. Operator-provisioned and `adminproxy-` credentials remain
  server-only. Create, rotate, and revoke write immutable lifecycle audit
  records with the human actor and role snapshot. Persistent SDK credentials
  keep `actor_user_id` null so their traffic is not misattributed to the human
  who created them.
- Unsafe requests require an exact allowed `Origin`, a CSRF cookie, a matching
  header, and the session-bound CSRF digest.
- `/api/projects/{project_id}/{service}/...` is deny-by-default. The proxy
  verifies project membership and the required canonical APDL role before
  injecting a server-side API key.
- Caller-supplied API keys, authorization headers, cookies, internal tokens,
  and project assertions for another tenant are discarded or rejected.
- Uvicorn preserves the socket peer instead of trusting forwarded headers.
  The Admin nginx edge clears `Forwarded` and `X-Real-IP`, overwrites
  `X-Forwarded-For` with its direct peer, and is the only network allowed to
  supply that single-hop identity to the API.
- Login abuse controls never lock the shared account row. PostgreSQL applies
  atomic short-window global, network, and opaque-device budgets plus
  progressive network-and-device delays for each submitted email. A correct
  password from an unthrottled source remains valid regardless of the
  account-wide risk count.
- Raw IP addresses, submitted emails, and device tokens are not persisted in
  login-risk tables. They are keyed with a deployment-specific HMAC secret.
  The opaque `apdl_admin_device` cookie is HttpOnly, SameSite Strict, and is
  used only as a risk signal—not as authentication.
- Fifty failures for one active account within 24 hours create one durable,
  deduplicated `suspicious_login_activity` notification. The signed-in user
  sees it in the console and can acknowledge it; the alert does not deny
  access.
- Projects without a configured long-lived service key use a random five-minute
  proxy credential. Only its SHA-256 hash is inserted in PostgreSQL, the raw key
  exists only for the upstream request, and the credential is deleted when the
  response or SSE stream closes.
- Every authorized mutation gets a fail-closed `admin_proxy_audit` attempt row
  with human user, project, role, service, route, and final status. Bodies and
  credentials are deliberately excluded.

## Local setup

```bash
make deps
make migrate-postgres
make run-admin-api
make run-admin
```

Open `http://localhost:5173/register` and create an account. Registration starts
an authenticated session with an empty project list. The user may create a
core analytics project from the workspace settings, then create a restricted
browser or confidential SDK credential from the same page. Copy the revealed
key before closing the dialog; it cannot be recovered. An operator may instead
grant membership to an operator-provisioned project. Self-created projects
expose Agents history read-only by default and cannot execute LLM or Codegen
work until an operator records an explicit project execution authorization.

Managed credential routes are:

```text
GET  /api/projects/{project_id}/credentials
POST /api/projects/{project_id}/credentials
POST /api/projects/{project_id}/credentials/{credential_id}/rotate
POST /api/projects/{project_id}/credentials/{credential_id}/revoke
GET  /api/projects/{project_id}/credentials/{credential_id}/audit
```

Create accepts only `credential_kind` and an explicit canonical `roles` list.
Rotate and revoke accept only an empty JSON object. Unsafe operations require
the normal exact-origin and CSRF checks.

`make create-admin-user` remains available for bootstrap, recovery, and
non-browser provisioning. It prompts for the password without placing it in
shell history; `--password-stdin` supports secret-manager pipelines. Granting
`agents:run`, `agents:manage`, or `agents:approve` on a self-created project
requires a deliberate, durable override:

```bash
make create-admin-user ARGS="\
  --email operator@example.com \
  --project-id acme \
  --roles agents:manage \
  --allow-self-registered-execution \
  --override-actor operator@example.com \
  --override-reason 'Approved production automation boundary'"
```

The actor, reason, source, and timestamp are stored in an immutable project
authorization row in the same transaction as the role grant. Omit the override
flags for operator-provisioned or already-authorized projects. Remove execution
roles and revoke credentials to stop access; the provenance record itself is
not rewritten.

## Health and readiness

- `GET /api/health` is process liveness only.
- `GET /api/ready` returns one strict payload with `core` and `capabilities`
  maps. PostgreSQL, Ingestion, and Config are core; failure of any core check
  returns HTTP 503 with `status: "not_ready"`.
- Query, Agents, and Codegen are projected as optional capabilities. Their
  failure retains HTTP 200 and core `status: "ready"`, while setting
  `degraded: true` and the affected capability to `not_ready`.
- All upstream probes use the short readiness timeout and run concurrently, so
  an unavailable optional service does not serialize or extend the health path.

Example degraded response:

```json
{
  "status": "ready",
  "degraded": true,
  "core": {
    "postgres": "ready",
    "ingestion": "ready",
    "config": "ready"
  },
  "capabilities": {
    "query": "ready",
    "agents": "not_ready",
    "codegen": "not_ready"
  }
}
```

## Configuration

| Variable | Purpose |
|---|---|
| `POSTGRES_URL` | Admin users, memberships, and sessions |
| `APDL_SERVICE_API_KEYS` | JSON object of project-scoped service keys; server-only |
| `APDL_DEV_API_KEY` | Explicit local-only credential provisioned by `make migrate-postgres` |
| `INGESTION_SERVICE_URL` | Private ingestion URL |
| `CONFIG_SERVICE_URL` | Private config URL |
| `QUERY_SERVICE_URL` | Private query URL |
| `AGENTS_SERVICE_URL` | Private agents URL |
| `CODEGEN_SERVICE_URL` | Private codegen URL |
| `APDL_ADMIN_ALLOWED_ORIGINS` | JSON array of exact console origins; local defaults cover ports 5173 and 5174, and wildcards are rejected |
| `APDL_ADMIN_COOKIE_SECURE` | Must be `true` in HTTPS deployments |
| `APDL_ADMIN_TRUSTED_PROXY_CIDRS` | JSON array of exact proxy networks allowed to supply one canonical `X-Forwarded-For` address; default is empty |
| `APDL_ADMIN_LOGIN_RISK_HMAC_KEY` | Deployment-unique secret, at least 32 bytes, used to pseudonymize login-risk identities |
| `APDL_ADMIN_LOGIN_RATE_WINDOW_SECONDS` | Global/network/device request-budget window; default 60 seconds |
| `APDL_ADMIN_LOGIN_GLOBAL_RATE_LIMIT` | Global login attempts per window; default 600 |
| `APDL_ADMIN_LOGIN_NETWORK_RATE_LIMIT` | Login attempts per canonical client network address per window; default 30 |
| `APDL_ADMIN_LOGIN_DEVICE_RATE_LIMIT` | Login attempts per opaque device cookie per window; default 20 |
| `APDL_ADMIN_LOGIN_PROGRESSIVE_FAILURE_THRESHOLD` | Per-email network/device failure count that starts progressive delay; default 3 |
| `APDL_ADMIN_LOGIN_PROGRESSIVE_BASE_DELAY_SECONDS` | Initial progressive delay; default 1 second |
| `APDL_ADMIN_LOGIN_PROGRESSIVE_MAX_DELAY_SECONDS` | Progressive delay cap; default 60 seconds |
| `APDL_ADMIN_LOGIN_ACCOUNT_NOTICE_THRESHOLD` | Account-wide failure count that creates a notification; default 50 |
| `APDL_ADMIN_LOGIN_ACCOUNT_RISK_WINDOW_SECONDS` | Account notification window; default 24 hours |
| `APDL_ADMIN_LOGIN_DEVICE_TTL_SECONDS` | Opaque risk-cookie lifetime; default one year |
| `APDL_ADMIN_SESSION_TTL_SECONDS` | Absolute session lifetime; default 8 hours |
| `APDL_ADMIN_SESSION_IDLE_SECONDS` | Idle expiry; default 30 minutes |
| `APDL_ADMIN_STREAM_AUTH_CHECK_SECONDS` | Current session, membership, and role revalidation interval; default 5 seconds |
| `APDL_ADMIN_UPSTREAM_READ_TIMEOUT_SECONDS` | Upstream per-read timeout; default 60 seconds, comfortably above Config heartbeats |
| `APDL_ADMIN_READINESS_PROBE_TIMEOUT_SECONDS` | Per-dependency readiness probe timeout; default 2 seconds |

## Verification

```bash
make lint-admin-api
make test-admin-api
```
