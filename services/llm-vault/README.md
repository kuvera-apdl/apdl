# Project LLM Credential Vault

Private FastAPI service on port 8086 that is the single custodian for project
LLM provider credentials used by Agents and Codegen.

The vault owns the only database login that can read
`llm_vault_provider_secrets` and the only deployment copy of the AES-256-GCM
encryption key. Agents and Codegen keep separate non-secret model, policy, and
assignment projections. At provider egress they request one exact credential
ID/version with an execution ID and purpose; the vault verifies the explicit
consumer grant and appends an immutable access-audit row before returning the
plaintext over the private service network.

## Configuration

| Variable | Purpose |
|---|---|
| `POSTGRES_URL` | `apdl_llm_vault` database URL; must not use `apdl_runtime` |
| `LLM_VAULT_ENCRYPTION_KEY_BASE64` | Standard Base64 encoding of exactly 32 random bytes |
| `LLM_VAULT_ADMIN_TOKEN` | Admin API proxy authentication |
| `LLM_VAULT_AGENTS_TOKEN` | Agents workload authentication |
| `LLM_VAULT_CODEGEN_TOKEN` | Codegen workload authentication |
| `LLM_VAULT_PROJECTION_TOKEN` | Vault authentication to private consumer projection endpoints |
| `AGENTS_SERVICE_URL` | Private Agents URL used for model projection |
| `CODEGEN_SERVICE_URL` | Private Codegen URL used for model projection |

Generate production tokens independently and generate the encryption key with
`openssl rand -base64 32`. Do not reuse a workload token as the admin or
projection token. Local `make setup` generates the encryption key and admin
token once and preserves both on later setup runs; deployments must provision
their own independent values.

## API boundaries

- Admin lifecycle: `/v1/llm-connections` and connection-specific replace,
  refresh, and revoke routes. The Admin API injects the authenticated actor and
  project assertions.
- Workload access: `POST /internal/v1/credential-access`. The request must name
  the consumer, exact credential ID/version, execution ID, and purpose.
- Consumer projections: the vault calls the private Agents and Codegen
  projection endpoints before opening its database transaction. Provider
  discovery or either requested projection failure leaves all state unchanged.

Migration `056_project_llm_credential_vault.sql` is a fresh-install-only custody
cutover. Any legacy Agents or Codegen credential row, including replaced or
revoked history, blocks it because the old ciphertext and credential lineage
cannot be rebound safely to the new empty vault. Revocation crypto-shreds the
secret bytes but deliberately retains that history, so it is not a supported
remediation. Initialize a fresh PostgreSQL database, apply the canonical
migrations, and reconnect providers through the shared vault. There is no
in-place conversion or dual-schema compatibility mode.

## Local development

```bash
make dev
make run-llm-vault
```

The service exposes `/health` for liveness and `/ready` for database readiness.

## Encryption-key rotation

Rotation is one vault-wide, all-or-nothing maintenance operation. Drain every
APDL PostgreSQL runtime while leaving PostgreSQL online, set the vault database
URL plus the old and new 32-byte keys, then run:

```bash
export POSTGRES_URL='postgresql://apdl_llm_vault:...@localhost:5432/apdl'
export LLM_VAULT_OLD_ENCRYPTION_KEY_BASE64='...'
export LLM_VAULT_NEW_ENCRYPTION_KEY_BASE64='...'
make rotate-llm-vault-key ARGS='--operator operator@example.com'
```

The command acquires both exclusive APDL maintenance barriers, authenticates
every active ciphertext before writing, re-encrypts the full set in one
transaction, verifies the result, and appends immutable per-credential audit
rows. CPython cannot zeroize the immutable strings returned by decryption.
Rotation therefore scopes plaintext to one credential preparation call at a
time, retains only encrypted plans, and decrypts each credential once with the
old key plus once with the new key for verification. Update
`LLM_VAULT_ENCRYPTION_KEY_BASE64` to the new value before restarting services.
The old and new rotation keys are command-only variables; they are never passed
to Agents, Codegen, or the normal vault service process.
