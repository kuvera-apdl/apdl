-- Automatic guardrail mutation is unsupported in the OSS developer preview.
-- Keep the persisted contract fail-closed so no stale true value can become a
-- latent mutation trigger if an experimental worker is started accidentally.
ALTER TABLE flags ALTER COLUMN auto_disable SET DEFAULT false;

UPDATE flags
SET auto_disable = false,
    version = version + 1,
    updated_at = now()
WHERE auto_disable = true;
