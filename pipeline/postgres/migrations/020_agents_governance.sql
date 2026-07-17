-- Feature proposal identifiers are authored by an LLM and are therefore only
-- unique inside a project.  The original global primary key let one project's
-- proposal block another project and made unscoped fallback reads ambiguous.
ALTER TABLE feature_proposals
    DROP CONSTRAINT feature_proposals_pkey;

ALTER TABLE feature_proposals
    ADD CONSTRAINT feature_proposals_pkey
    PRIMARY KEY (project_id, proposal_id);
