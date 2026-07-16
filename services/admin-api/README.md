# APDL Admin API

Backend-for-frontend for the APDL Admin Console. It is the browser security
boundary: human credentials terminate here, service credentials remain here,
and every proxied request is authorized against a user, project, and role.

## Security model

- Administrator passwords use Argon2id and are never returned by an API.
- Login creates a random opaque session. PostgreSQL stores only its SHA-256
  digest; the browser receives the raw value in an `HttpOnly`,
  `SameSite=Strict` cookie.
- Sessions have absolute and idle expiry. Logout and password reprovisioning
  revoke server-side sessions.
- Long-lived SSE connections re-check the database on upstream heartbeats and
  emit `auth_expired` before closing when the session is no longer valid.
- Five consecutive failures lock an account for 15 minutes by default. Login
  failures use one generic response to avoid user enumeration.
- Unsafe requests require an exact allowed `Origin`, a CSRF cookie, a matching
  header, and the session-bound CSRF digest.
- `/api/projects/{project_id}/{service}/...` is deny-by-default. The proxy
  verifies project membership and the required canonical APDL role before
  injecting a server-side API key.
- Caller-supplied API keys, authorization headers, cookies, internal tokens,
  and project assertions for another tenant are discarded or rejected.
- Every authorized mutation gets a fail-closed `admin_proxy_audit` attempt row
  with human user, project, role, service, route, and final status. Bodies and
  credentials are deliberately excluded.

## Local setup

```bash
make deps
make migrate-postgres
make create-admin-user ARGS="--email admin@example.com --project-id apdl --roles config:read config:write query:read agents:read"
make run-admin-api
make run-admin
```

The provisioning command prompts for the password without placing it in shell
history. Pass `--password-stdin` for a secret-manager pipeline. Running it again
changes the password, updates that project's roles, and revokes existing
sessions.

## Configuration

| Variable | Purpose |
|---|---|
| `POSTGRES_URL` | Admin users, memberships, and sessions |
| `APDL_SERVICE_API_KEYS` | JSON object of project-scoped service keys; server-only |
| `APDL_DEV_API_KEY` | Explicit local-only credential provisioned by `make migrate-postgres` |
| `APDL_INTERNAL_TOKEN` | Server-only codegen credential |
| `INGESTION_SERVICE_URL` | Private ingestion URL |
| `CONFIG_SERVICE_URL` | Private config URL |
| `QUERY_SERVICE_URL` | Private query URL |
| `AGENTS_SERVICE_URL` | Private agents URL |
| `CODEGEN_SERVICE_URL` | Private codegen URL |
| `APDL_ADMIN_ALLOWED_ORIGINS` | JSON array of exact console origins; wildcards are rejected |
| `APDL_ADMIN_COOKIE_SECURE` | Must be `true` in HTTPS deployments |
| `APDL_ADMIN_SESSION_TTL_SECONDS` | Absolute session lifetime; default 8 hours |
| `APDL_ADMIN_SESSION_IDLE_SECONDS` | Idle expiry; default 30 minutes |

## Verification

```bash
make lint-admin-api
make test-admin-api
```
