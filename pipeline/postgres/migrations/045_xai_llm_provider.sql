-- Add xAI as one strict, auditable LLM provider identity.
--
-- Provider/model/endpoint policy remains project-scoped and exact. This
-- migration only permits the canonical `xai` provider value in the policy and
-- attempt ledgers; it does not grant any project permission or create a paid
-- provider policy.

ALTER TABLE llm_project_provider_policies
    DROP CONSTRAINT IF EXISTS llm_project_provider_name_check,
    ADD CONSTRAINT llm_project_provider_name_check
        CHECK (provider IN ('openai', 'anthropic', 'google', 'xai', 'local'));

ALTER TABLE llm_provider_attempts
    DROP CONSTRAINT IF EXISTS llm_provider_attempts_provider_check,
    ADD CONSTRAINT llm_provider_attempts_provider_check
        CHECK (provider IN ('openai', 'anthropic', 'google', 'xai', 'local'))
        NOT VALID;
ALTER TABLE llm_provider_attempts
    VALIDATE CONSTRAINT llm_provider_attempts_provider_check;
