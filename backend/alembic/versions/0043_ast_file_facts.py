# ruff: noqa: E501
"""Immutable, worker-fenced per-file AST facts."""

from alembic import op

revision = "0043_ast_file_facts"
down_revision = "0042_public_github_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    SET LOCAL lock_timeout='5s';
    CREATE TABLE public.ast_file_facts (
      org_id UUID NOT NULL, snapshot_id UUID NOT NULL, run_id UUID NOT NULL, file_id UUID NOT NULL,
      repository_id UUID NOT NULL, spec_id UUID NOT NULL, config_hash TEXT NOT NULL,
      language TEXT NOT NULL, status TEXT NOT NULL,
      facts_json JSONB NOT NULL, facts_sha256 TEXT NOT NULL,
      symbol_count INTEGER NOT NULL, import_count INTEGER NOT NULL, export_count INTEGER NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(org_id,snapshot_id,run_id,file_id),
      CONSTRAINT fk_ast_facts_run FOREIGN KEY(org_id,snapshot_id,run_id)
        REFERENCES public.analysis_runs(org_id,snapshot_id,id) ON DELETE RESTRICT,
      CONSTRAINT fk_ast_facts_file FOREIGN KEY(org_id,snapshot_id,file_id)
        REFERENCES public.source_files(org_id,snapshot_id,id) ON DELETE RESTRICT,
      CONSTRAINT fk_ast_facts_spec FOREIGN KEY(org_id,repository_id,snapshot_id,spec_id,config_hash)
        REFERENCES public.analysis_specs(org_id,repository_id,snapshot_id,id,config_hash) ON DELETE RESTRICT,
      CONSTRAINT ck_ast_facts_json CHECK(jsonb_typeof(facts_json)='object' AND octet_length(facts_json::text)<=1048576),
      CONSTRAINT ck_ast_facts_hashes CHECK(config_hash ~ '^[0-9a-f]{64}$' AND facts_sha256 ~ '^[0-9a-f]{64}$'),
      CONSTRAINT ck_ast_facts_status CHECK(status IN ('parsed','partial')),
      CONSTRAINT ck_ast_facts_counts CHECK(symbol_count>=0 AND import_count>=0 AND export_count>=0),
      CONSTRAINT ck_ast_facts_language_bytes CHECK(octet_length(language) BETWEEN 1 AND 64)
    );
    CREATE INDEX ix_ast_file_facts_run ON public.ast_file_facts(org_id,snapshot_id,run_id,file_id);
    CREATE INDEX ix_ast_file_facts_history ON public.ast_file_facts(org_id,repository_id,snapshot_id,created_at,run_id);
    CREATE TRIGGER guard_ast_facts_immutable BEFORE UPDATE OR DELETE ON public.ast_file_facts
      FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
    CREATE FUNCTION public.codespy_ast_facts_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
    DECLARE jid uuid; j public.jobs%ROWTYPE; r public.analysis_runs%ROWTYPE; s public.analysis_specs%ROWTYPE;
    BEGIN
      jid:=public.codespy_phase2_authority();
      SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
      SELECT * INTO r FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.run_id FOR UPDATE;
      SELECT * INTO s FROM public.analysis_specs WHERE org_id=NEW.org_id AND id=NEW.spec_id;
      IF j.id IS NULL OR r.id IS NULL OR s.id IS NULL OR j.operation<>'analyze_structure'
        OR j.analysis_spec_id IS DISTINCT FROM NEW.spec_id OR j.snapshot_id IS DISTINCT FROM NEW.snapshot_id
        OR j.repository_id IS DISTINCT FROM NEW.repository_id OR j.config_hash IS DISTINCT FROM NEW.config_hash
        OR r.status<>'running' OR r.worker_job_id IS DISTINCT FROM jid
        OR r.worker_attempt_id IS DISTINCT FROM j.current_attempt_id OR r.worker_fence IS DISTINCT FROM j.fence
        OR r.config_hash IS DISTINCT FROM NEW.config_hash
        OR s.config_hash IS DISTINCT FROM NEW.config_hash OR s.snapshot_id IS DISTINCT FROM NEW.snapshot_id
        OR s.repository_id IS DISTINCT FROM NEW.repository_id
        OR s.principal_id IS DISTINCT FROM j.principal_id OR r.config IS DISTINCT FROM s.config_json
        OR NOT (s.config_json->'adapters') ? 'ast_facts'
        OR NOT EXISTS(SELECT 1 FROM public.source_snapshots ss WHERE ss.org_id=NEW.org_id AND ss.id=NEW.snapshot_id AND ss.state='sealed')
        OR r.analyzer_name<>'codespy-structure' OR r.analyzer_version<>'1'
      THEN RAISE EXCEPTION 'foreign AST facts publication' USING ERRCODE='23514',CONSTRAINT='ast_facts_fence'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_ast_facts BEFORE INSERT ON public.ast_file_facts
      FOR EACH ROW EXECUTE FUNCTION public.codespy_ast_facts_guard();
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.ast_file_facts) THEN
      RAISE EXCEPTION 'AST facts history must be preserved' USING ERRCODE='23514'; END IF; END $$;
    DROP TRIGGER guard_ast_facts ON public.ast_file_facts;
    DROP FUNCTION public.codespy_ast_facts_guard();
    DROP TABLE public.ast_file_facts;
    """)
