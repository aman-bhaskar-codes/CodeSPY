"""Durable post-publication memory intents and immutable processing receipts."""

from alembic import op

revision = "0036_report_memory_outbox"
down_revision = "0035_embedding_budget_authority"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.outbox DROP CONSTRAINT ck_outbox_kind;
ALTER TABLE public.outbox ADD CONSTRAINT ck_outbox_kind
 CHECK(kind IN ('wakeup','dead_letter','memory_proposals'));
CREATE TABLE public.report_memory_receipts(
 org_id uuid NOT NULL,intent_id uuid NOT NULL,report_id uuid,report_digest text,
 derivation_revision integer NOT NULL CHECK(derivation_revision=1),
 outcome text NOT NULL CHECK(outcome IN ('created','no_candidates','blocked')),
 reason text CHECK(reason IN ('invalid_origin','access_or_source_unavailable',
  'processing_failed','unsupported_or_capped')),
 candidate_ids jsonb NOT NULL CHECK(jsonb_typeof(candidate_ids)='array'
  AND jsonb_array_length(candidate_ids)<=10),
 omitted_claims integer NOT NULL DEFAULT 0 CHECK(omitted_claims BETWEEN 0 AND 50),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,intent_id),UNIQUE(org_id,report_id,derivation_revision),
 FOREIGN KEY(org_id,intent_id) REFERENCES public.outbox(org_id,id),
 FOREIGN KEY(org_id,report_id) REFERENCES public.investigation_reports(org_id,id)
);
CREATE TRIGGER guard_report_memory_receipt_history BEFORE UPDATE OR DELETE
 ON public.report_memory_receipts FOR EACH ROW
 EXECUTE FUNCTION public.codespy_immutable_record_guard();
CREATE FUNCTION public.codespy_report_memory_receipt_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE job uuid; inv public.investigations%ROWTYPE;
BEGIN
 SELECT job_id INTO job FROM public.outbox WHERE org_id=NEW.org_id AND id=NEW.intent_id
  AND kind='memory_proposals';
 IF job IS NULL THEN RAISE EXCEPTION 'memory effect scope differs' USING ERRCODE='23514'; END IF;
 IF NEW.report_id IS NOT NULL THEN
  SELECT i.* INTO inv FROM public.investigations i JOIN public.investigation_reports r
   ON r.org_id=i.org_id AND r.investigation_id=i.id
   WHERE r.org_id=NEW.org_id AND r.id=NEW.report_id AND r.job_id=job
    AND r.report_digest=NEW.report_digest AND i.final_report_id=r.id AND i.status='completed';
  IF NOT FOUND THEN RAISE EXCEPTION 'memory report scope differs' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements_text(NEW.candidate_ids) c(id)
   WHERE NOT EXISTS(SELECT 1 FROM public.repo_memories m WHERE m.org_id=NEW.org_id
    AND m.id=c.id::uuid AND m.repository_id=inv.repository_id AND m.snapshot_id=inv.snapshot_id
    AND m.creator_id=inv.principal_id AND m.state='candidate')) THEN
   RAISE EXCEPTION 'memory candidate scope differs' USING ERRCODE='23514'; END IF;
 ELSIF NEW.outcome<>'blocked' OR jsonb_array_length(NEW.candidate_ids)<>0 THEN
  RAISE EXCEPTION 'memory origin required' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_report_memory_receipt_guard() FROM PUBLIC;
CREATE TRIGGER guard_report_memory_receipt_insert BEFORE INSERT ON public.report_memory_receipts
 FOR EACH ROW EXECUTE FUNCTION public.codespy_report_memory_receipt_guard();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT SELECT,INSERT ON public.report_memory_receipts TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT SELECT ON public.report_memory_receipts TO p2_api;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("report memory processing history must be retained")
