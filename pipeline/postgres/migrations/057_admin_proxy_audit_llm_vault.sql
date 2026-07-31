-- Permit the Admin API to audit LLM Vault mutations before proxying them.

ALTER TABLE admin_proxy_audit
    DROP CONSTRAINT admin_proxy_audit_service_check;

ALTER TABLE admin_proxy_audit
    ADD CONSTRAINT admin_proxy_audit_service_check CHECK (
        service IN (
            'ingestion', 'config', 'query', 'agents', 'codegen', 'llm-vault'
        )
    );
