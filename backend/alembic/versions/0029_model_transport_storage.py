"""Preserve usage evidence and encrypted model protocol state."""

from alembic import op

revision = "0029_model_transport_storage"
down_revision = "0028_accounting_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
CREATE TABLE public.provider_usage_receipts (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,reservation_id uuid NOT NULL,
 profile_digest varchar(64) NOT NULL CHECK(profile_digest ~ '^[a-f0-9]{64}$'),
 payload_digest varchar(64) NOT NULL CHECK(payload_digest ~ '^[a-f0-9]{64}$'),
 quality varchar(32) NOT NULL CHECK(quality IN ('unknown','measured','aggregate_output')),
 evidence_json jsonb NOT NULL CHECK(octet_length(evidence_json::text)<=20000),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(org_id,id),
 CONSTRAINT uq_provider_usage_evidence UNIQUE(org_id,reservation_id,payload_digest),
 FOREIGN KEY(org_id,reservation_id) REFERENCES public.model_reservations(org_id,reservation_id)
);
CREATE UNIQUE INDEX uq_provider_final_usage ON public.provider_usage_receipts(org_id,reservation_id)
 WHERE quality<>'unknown';
CREATE TABLE public.model_continuations (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,reservation_id uuid NOT NULL,
 investigation_id uuid NOT NULL,principal_id uuid NOT NULL,
 profile_digest varchar(64) NOT NULL CHECK(profile_digest ~ '^[a-f0-9]{64}$'),
 ciphertext bytea NOT NULL CHECK(octet_length(ciphertext) BETWEEN 28 AND 262172),
 expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(expires_at>created_at AND expires_at<=created_at+interval '7 days 1 second'),
 PRIMARY KEY(org_id,id),
 FOREIGN KEY(org_id,reservation_id) REFERENCES public.model_reservations(org_id,reservation_id)
);
CREATE FUNCTION public.codespy_model_transport_guard() RETURNS trigger
 LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE r public.model_reservations; l public.model_spend_ledger;
BEGIN
 IF TG_OP<>'INSERT' THEN
  IF TG_TABLE_NAME='model_continuations' AND TG_OP='DELETE'
     AND OLD.expires_at<=clock_timestamp() THEN RETURN OLD; END IF;
  RAISE EXCEPTION 'model transport history immutable' USING ERRCODE='23514';
 END IF;
 SELECT * INTO r FROM public.model_reservations
 WHERE org_id=NEW.org_id AND reservation_id=NEW.reservation_id;
 IF NOT FOUND OR r.repository_id<>NEW.repository_id THEN
  RAISE EXCEPTION 'model transport scope mismatch' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='model_continuations' THEN
  IF r.principal_id<>NEW.principal_id OR r.investigation_id<>NEW.investigation_id THEN
   RAISE EXCEPTION 'model continuation scope mismatch' USING ERRCODE='23514'; END IF;
 ELSE
  IF NEW.quality<>NEW.evidence_json->>'quality' THEN
   RAISE EXCEPTION 'model usage quality mismatch' USING ERRCODE='23514'; END IF;
  IF NEW.quality<>'unknown' THEN
   SELECT * INTO l FROM public.model_spend_ledger WHERE org_id=NEW.org_id
    AND reservation_id=NEW.reservation_id AND NOT is_uncertain_hold;
   IF NOT FOUND OR r.state<>'settled' OR NEW.evidence_json->'usage'<>
    jsonb_build_object('input_tokens',l.input_tokens,'output_tokens',l.output_tokens,
      'thinking_tokens',l.thinking_tokens,'cache_read_tokens',l.cache_read_tokens,
      'cache_write_tokens',l.cache_write_tokens) THEN
    RAISE EXCEPTION 'usage evidence does not match settlement' USING ERRCODE='23514'; END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_provider_usage_receipts BEFORE INSERT OR UPDATE OR DELETE
 ON public.provider_usage_receipts FOR EACH ROW EXECUTE FUNCTION
 public.codespy_model_transport_guard();
CREATE TRIGGER guard_model_continuations BEFORE INSERT OR UPDATE OR DELETE
 ON public.model_continuations FOR EACH ROW EXECUTE FUNCTION public.codespy_model_transport_guard();
REVOKE ALL ON public.provider_usage_receipts,public.model_continuations FROM PUBLIC;
""")


def downgrade() -> None:
    op.execute("""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.provider_usage_receipts)
 OR EXISTS(SELECT 1 FROM public.model_continuations) THEN
  RAISE EXCEPTION 'cannot erase model transport history' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.provider_usage_receipts,public.model_continuations;
DROP FUNCTION public.codespy_model_transport_guard();
""")
