"""Bind report publication to live worker authority and complete terminal state."""

from alembic import op

revision = "0033_investigation_publication_guards"
down_revision = "0032_reviewed_memory"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
CREATE FUNCTION public.codespy_investigation_report_insert_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE;
BEGIN
 jid := public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 IF j.id IS NULL OR j.operation<>'investigate_snapshot' OR NEW.job_id<>jid
    OR NEW.investigation_id IS DISTINCT FROM j.investigation_id
    OR NEW.spec_hash IS DISTINCT FROM j.spec_hash THEN
   RAISE EXCEPTION 'report worker scope differs' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_investigation_report_insert_guard() FROM PUBLIC;
CREATE TRIGGER guard_investigation_report_insert BEFORE INSERT ON public.investigation_reports
 FOR EACH ROW EXECUTE FUNCTION public.codespy_investigation_report_insert_guard();
CREATE TRIGGER guard_investigation_receipt_insert
 BEFORE INSERT ON public.investigation_publication_receipts
 FOR EACH ROW EXECUTE FUNCTION public.codespy_investigation_report_insert_guard();

CREATE FUNCTION public.codespy_investigation_publication_complete()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE j public.jobs%ROWTYPE; jid uuid; oid uuid;
BEGIN
 oid:=NEW.org_id;
 IF TG_TABLE_NAME='jobs' THEN
   IF NEW.operation<>'investigate_snapshot' OR NEW.state NOT IN ('succeeded','partial') THEN
     RETURN NEW;
   END IF;
   jid:=NEW.id;
 ELSIF TG_TABLE_NAME='investigations' THEN
   IF NEW.spec_hash IS NULL THEN RETURN NEW; END IF;
   IF NEW.status<>'completed' THEN
     IF NEW.final_report_id IS NOT NULL OR OLD.final_report_id IS NOT NULL THEN
       RAISE EXCEPTION 'published summary is immutable' USING ERRCODE='23514';
     END IF;
     RETURN NEW;
   END IF;
   jid:=NEW.current_job_id;
 ELSE
   jid:=NEW.job_id;
 END IF;
 SELECT * INTO j FROM public.jobs WHERE org_id=oid AND id=jid;
 IF j.id IS NULL OR j.state NOT IN ('succeeded','partial') OR NOT EXISTS (
   SELECT 1 FROM public.investigation_reports r
   JOIN public.investigation_publication_receipts p
    ON p.org_id=r.org_id AND p.report_id=r.id AND p.job_id=r.job_id
   JOIN public.investigations i ON i.org_id=r.org_id AND i.id=r.investigation_id
   JOIN public.job_attempts a ON a.org_id=j.org_id AND a.job_id=j.id AND a.id=j.current_attempt_id
   WHERE r.org_id=oid AND r.job_id=jid AND r.id=j.result_report_id
    AND r.spec_hash=j.spec_hash AND r.investigation_id=j.investigation_id
    AND p.spec_hash=j.spec_hash AND p.investigation_id=j.investigation_id
    AND p.attempt_id=j.current_attempt_id AND p.fencing_token=j.fence
    AND p.report_digest=r.report_digest AND a.state='succeeded' AND a.fence=j.fence
    AND i.current_job_id=j.id AND i.final_report_id=r.id AND i.status='completed'
    AND i.verdict=r.verdict AND i.summary=r.summary
    AND i.verdict_confidence=r.verdict_confidence
    AND a.lease_expires_at>clock_timestamp() AND j.deadline>clock_timestamp()
 ) THEN
   RAISE EXCEPTION 'incomplete investigation publication' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_investigation_publication_complete() FROM PUBLIC;
CREATE CONSTRAINT TRIGGER guard_investigation_job_complete AFTER UPDATE ON public.jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.codespy_investigation_publication_complete();
CREATE CONSTRAINT TRIGGER guard_investigation_report_complete
 AFTER INSERT ON public.investigation_reports
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.codespy_investigation_publication_complete();
CREATE CONSTRAINT TRIGGER guard_investigation_receipt_complete
 AFTER INSERT ON public.investigation_publication_receipts
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 EXECUTE FUNCTION public.codespy_investigation_publication_complete();
CREATE CONSTRAINT TRIGGER guard_investigation_summary_complete
 AFTER UPDATE ON public.investigations DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
 WHEN (NEW.spec_hash IS NOT NULL)
 EXECUTE FUNCTION public.codespy_investigation_publication_complete();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT UPDATE(status,final_report_id,verdict,verdict_confidence,summary,updated_at)
   ON public.investigations TO p2_broker;
 END IF;
END $$;
"""

DOWN = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.investigation_reports) THEN
  RAISE EXCEPTION 'publication authority history must be preserved';
 END IF;
END $$;
DROP TRIGGER guard_investigation_summary_complete ON public.investigations;
DROP TRIGGER guard_investigation_job_complete ON public.jobs;
DROP TRIGGER guard_investigation_report_complete ON public.investigation_reports;
DROP TRIGGER guard_investigation_receipt_complete ON public.investigation_publication_receipts;
DROP TRIGGER guard_investigation_report_insert ON public.investigation_reports;
DROP TRIGGER guard_investigation_receipt_insert ON public.investigation_publication_receipts;
DROP FUNCTION public.codespy_investigation_report_insert_guard();
DROP FUNCTION public.codespy_investigation_publication_complete();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  REVOKE UPDATE(status,final_report_id,verdict,verdict_confidence,summary,updated_at)
   ON public.investigations FROM p2_broker;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN)
