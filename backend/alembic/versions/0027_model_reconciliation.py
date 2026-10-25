"""Append-only usage receipts tied to durable model settlement."""

from alembic import op

revision = "0027_model_reconciliation"
down_revision = "0026_model_cache_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
CREATE TABLE public.model_usage_reconciliations (
 org_id uuid NOT NULL, id uuid NOT NULL, repository_id uuid NOT NULL,
 reservation_id uuid NOT NULL, actor_principal_id uuid NOT NULL,
 request_key varchar(128) NOT NULL CHECK(length(request_key)>0),
 request_digest varchar(64) NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
 evidence_digest varchar(64) NOT NULL CHECK(evidence_digest ~ '^[a-f0-9]{64}$'),
 price_digest varchar(64) NOT NULL CHECK(price_digest ~ '^[a-f0-9]{64}$'),
 provider_request_id varchar(128) NOT NULL CHECK(length(provider_request_id)>0),
 usage_json jsonb NOT NULL CHECK(jsonb_typeof(usage_json)='object'),
 reason varchar(40) NOT NULL
 CHECK(reason IN ('provider_usage_receipt','operator_usage_confirmation')),
 charged_micros bigint NOT NULL CHECK(charged_micros>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 CONSTRAINT uq_model_reconcile_key UNIQUE(org_id,request_key),
 CONSTRAINT uq_model_reconcile_reservation UNIQUE(org_id,reservation_id),
 CONSTRAINT fk_model_reconcile_reservation FOREIGN KEY(org_id,reservation_id)
 REFERENCES public.model_reservations(org_id,reservation_id),
 CONSTRAINT fk_model_reconcile_actor FOREIGN KEY(org_id,actor_principal_id)
 REFERENCES public.memberships(org_id,principal_id),
 CONSTRAINT fk_model_reconcile_repo FOREIGN KEY(org_id,repository_id)
 REFERENCES public.repositories(org_id,id)
);
CREATE FUNCTION public.codespy_guard_model_reconciliation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP <> 'INSERT' THEN
  RAISE EXCEPTION 'usage reconciliation is immutable' USING ERRCODE='23514';
 END IF;
 IF NOT EXISTS (
 SELECT 1 FROM public.model_reservations r
 JOIN public.model_spend_ledger l ON l.org_id=r.org_id AND l.reservation_id=r.reservation_id
 WHERE r.org_id=NEW.org_id AND r.reservation_id=NEW.reservation_id
 AND r.repository_id=NEW.repository_id AND r.state='settled'
 AND r.settled_micros=NEW.charged_micros AND r.provider_request_id=NEW.provider_request_id
 AND public.codespy_scan_digest(r.price_snapshot)=NEW.price_digest
 AND NOT l.is_uncertain_hold AND l.charged_micros=NEW.charged_micros
 AND NEW.usage_json=jsonb_build_object('input_tokens',l.input_tokens,
  'output_tokens',l.output_tokens,'thinking_tokens',l.thinking_tokens,
  'cache_read_tokens',l.cache_read_tokens,'cache_write_tokens',l.cache_write_tokens)
 ) THEN
  RAISE EXCEPTION 'receipt does not match durable settlement' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_model_reconciliation BEFORE INSERT OR UPDATE OR DELETE
 ON public.model_usage_reconciliations FOR EACH ROW
 EXECUTE FUNCTION public.codespy_guard_model_reconciliation();
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.model_usage_reconciliations) THEN
  RAISE EXCEPTION 'cannot downgrade usage reconciliation history' USING ERRCODE='23514';
 END IF;
END $$;
DROP TABLE public.model_usage_reconciliations;
DROP FUNCTION public.codespy_guard_model_reconciliation();
""")
