"""Capture immutable model request digests and reservation pricing snapshots."""

from alembic import op

revision = "0024_model_request_identity"
down_revision = "0023_index_publication"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.model_reservations
 ADD COLUMN request_digest varchar(64),
 ADD COLUMN price_snapshot jsonb,
 ADD CONSTRAINT ck_model_request_pair CHECK (
   (request_digest IS NULL AND price_snapshot IS NULL)
   OR (request_digest IS NOT NULL AND price_snapshot IS NOT NULL)),
 ADD CONSTRAINT ck_model_request_digest CHECK (
   request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$'),
 ADD CONSTRAINT ck_model_price_scope CHECK (
   price_snapshot IS NULL OR (
     jsonb_typeof(price_snapshot)='object'
     AND octet_length(price_snapshot::text)<=4096
     AND price_snapshot->>'provider' IS NOT DISTINCT FROM provider
     AND price_snapshot->>'model_id' IS NOT DISTINCT FROM model_id
     AND jsonb_typeof(price_snapshot->'version') IS NOT DISTINCT FROM 'string'
     AND price_snapshot ?& ARRAY['provider','model_id','version','input_micros_per_1k',
       'output_micros_per_1k','effective_at','expires_at']
   ));
CREATE FUNCTION public.codespy_model_request_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' THEN
   IF OLD.request_digest IS NOT NULL THEN
     RAISE EXCEPTION 'bound model reservation is immutable' USING ERRCODE='23514';
   END IF;
   RETURN OLD;
 END IF;
 IF ROW(NEW.request_digest,NEW.price_snapshot) IS DISTINCT FROM
    ROW(OLD.request_digest,OLD.price_snapshot) THEN
   RAISE EXCEPTION 'model request identity is immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.request_digest IS NOT NULL AND
    ROW(NEW.org_id,NEW.reservation_id,NEW.repository_id,NEW.principal_id,
        NEW.investigation_id,NEW.logical_call_id,NEW.attempt_id,NEW.provider,NEW.model_id,
        NEW.charge_category,NEW.idempotency_key,NEW.estimated_input_tokens,
        NEW.estimated_output_tokens,NEW.reserved_micros,NEW.created_at)
    IS DISTINCT FROM
    ROW(OLD.org_id,OLD.reservation_id,OLD.repository_id,OLD.principal_id,
        OLD.investigation_id,OLD.logical_call_id,OLD.attempt_id,OLD.provider,OLD.model_id,
        OLD.charge_category,OLD.idempotency_key,OLD.estimated_input_tokens,
        OLD.estimated_output_tokens,OLD.reserved_micros,OLD.created_at) THEN
   RAISE EXCEPTION 'model reservation binding is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER model_request_identity_guard BEFORE UPDATE OR DELETE
 ON public.model_reservations FOR EACH ROW
 EXECUTE FUNCTION public.codespy_model_request_immutable();
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DO $$
BEGIN
 IF EXISTS (SELECT 1 FROM public.model_reservations WHERE request_digest IS NOT NULL) THEN
   RAISE EXCEPTION 'cannot downgrade bound model accounting history' USING ERRCODE='23514';
 END IF;
END;
$$;
DROP TRIGGER model_request_identity_guard ON public.model_reservations;
DROP FUNCTION public.codespy_model_request_immutable();
ALTER TABLE public.model_reservations
 DROP CONSTRAINT ck_model_price_scope,
 DROP CONSTRAINT ck_model_request_digest,
 DROP CONSTRAINT ck_model_request_pair,
 DROP COLUMN price_snapshot,
 DROP COLUMN request_digest;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
