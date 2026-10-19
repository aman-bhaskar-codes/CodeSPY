# ruff: noqa: E501
"""Worker-fenced immutable structural publication."""

from alembic import op

revision = "0011_structural_authority"
down_revision = "0010_structural_specs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    SET LOCAL lock_timeout='5s';
    CREATE TRIGGER guard_spec_immutable BEFORE UPDATE OR DELETE ON public.analysis_specs
      FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
    CREATE TRIGGER guard_report_immutable BEFORE UPDATE OR DELETE ON public.structural_reports
      FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
    CREATE FUNCTION public.codespy_structural_report_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
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
        OR NOT EXISTS(SELECT 1 FROM public.source_snapshots ss WHERE ss.org_id=NEW.org_id AND ss.id=NEW.snapshot_id AND ss.state='sealed')
        OR r.analyzer_name<>'codespy-structure' OR r.analyzer_version<>'1'
      THEN RAISE EXCEPTION 'foreign structural publication' USING ERRCODE='23514',CONSTRAINT='structural_fence'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_structural_report BEFORE INSERT ON public.structural_reports
      FOR EACH ROW EXECUTE FUNCTION public.codespy_structural_report_guard();
    CREATE FUNCTION public.codespy_structural_job_guard() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (OLD.operation='analyze_structure' OR NEW.operation='analyze_structure') AND
        ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.principal_id,NEW.snapshot_id,NEW.analysis_spec_id,NEW.config_hash,NEW.operation,NEW.request_key,NEW.request_hash,NEW.submission_id)
        IS DISTINCT FROM ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.principal_id,OLD.snapshot_id,OLD.analysis_spec_id,OLD.config_hash,OLD.operation,OLD.request_key,OLD.request_hash,OLD.submission_id)
      THEN RAISE EXCEPTION 'immutable structural job binding' USING ERRCODE='23514'; END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER guard_structural_job BEFORE UPDATE ON public.jobs
      FOR EACH ROW EXECUTE FUNCTION public.codespy_structural_job_guard();
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.analysis_specs) OR EXISTS(SELECT 1 FROM public.structural_reports)
      OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='analyze_structure') THEN
      RAISE EXCEPTION 'structural history must be preserved'; END IF; END $$;
    DROP TRIGGER guard_structural_job ON public.jobs;
    DROP FUNCTION public.codespy_structural_job_guard();
    DROP TRIGGER guard_structural_report ON public.structural_reports;
    DROP FUNCTION public.codespy_structural_report_guard();
    DROP TRIGGER guard_report_immutable ON public.structural_reports;
    DROP TRIGGER guard_spec_immutable ON public.analysis_specs;
    """)
