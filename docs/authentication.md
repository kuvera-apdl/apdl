# Authentication and tenant authorization

APDL uses one canonical API-key contract across ingestion, config, query, and
agents. Send the credential in `X-API-Key`:

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

Operator and service-principal credentials use the same table and receive only
the roles they need. In production, internal agents and the query guardrail
monitor read a JSON object from `APDL_SERVICE_API_KEYS`, keyed by project, so
each automated call uses a tenant-scoped credential:

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
internal calls, and provision least-privilege credentials through their normal
secret-management workflow.
