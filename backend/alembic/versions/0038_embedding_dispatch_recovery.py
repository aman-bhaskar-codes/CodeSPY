"""Linearize embedding dispatch, cancellation and conservative crash recovery."""

from alembic import op

revision = "0038_embedding_dispatch_recovery"
down_revision = "0037_embedding_cancel_identity"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.embedding_reservations ADD COLUMN dispatch_intent_at timestamptz;
CREATE OR REPLACE FUNCTION public.codespy_embedding_reservation_history_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='DELETE' THEN
 RAISE EXCEPTION 'embedding accounting history is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.state IN ('committed','released') AND NEW IS DISTINCT FROM OLD THEN
 RAISE EXCEPTION 'terminal embedding accounting is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.state='released' AND (OLD.request_digest IS NULL OR OLD.dispatch_intent_at IS NOT NULL) THEN
 RAISE EXCEPTION 'embedding_cancellation_not_proven_unsent' USING ERRCODE='23514'; END IF;
 IF OLD.dispatch_intent_at IS NOT NULL AND NEW.dispatch_intent_at IS DISTINCT FROM
 OLD.dispatch_intent_at THEN
 RAISE EXCEPTION 'embedding dispatch identity is immutable' USING ERRCODE='23514'; END IF;
 IF (to_jsonb(NEW)-ARRAY['state','settled_micros','failure_reason','updated_at',
 'actual_tokens','outcome_digest','dispatch_intent_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['state','settled_micros','failure_reason','updated_at',
 'actual_tokens','outcome_digest','dispatch_intent_at']) THEN
 RAISE EXCEPTION 'embedding request and price are immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_claim_embedding_dispatch(oid uuid,rid uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.embedding_reservations%ROWTYPE;
BEGIN
 SELECT * INTO r FROM public.embedding_reservations WHERE org_id=oid AND reservation_id=rid
 FOR UPDATE;
 IF NOT FOUND OR r.state<>'reserved' OR r.request_digest IS NULL OR
 NOT public.codespy_model_actor_write(r.org_id,r.principal_id,r.repository_id) THEN
 RAISE EXCEPTION 'embedding_dispatch_denied' USING ERRCODE='23514'; END IF;
 IF r.dispatch_intent_at IS NOT NULL THEN RETURN jsonb_build_object('claimed',false); END IF;
 UPDATE public.embedding_reservations SET
 dispatch_intent_at=clock_timestamp(),updated_at=clock_timestamp()
 WHERE org_id=oid AND reservation_id=rid;
 RETURN jsonb_build_object('claimed',true);
END $$;
CREATE OR REPLACE FUNCTION public.codespy_cancel_embedding_v3(oid uuid,rid uuid,p_reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.embedding_reservations%ROWTYPE;
BEGIN
 SELECT * INTO r FROM public.embedding_reservations WHERE org_id=oid AND reservation_id=rid
 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'embedding_not_found' USING ERRCODE='23514'; END IF;
 IF r.request_digest IS NULL OR r.dispatch_intent_at IS NOT NULL THEN
 RAISE EXCEPTION 'embedding_cancellation_not_proven_unsent' USING ERRCODE='23514'; END IF;
 RETURN public.codespy_transition_embedding_v2(
  oid, rid, jsonb_build_object('status', 'cancel', 'reason', p_reason)
 );
END $$;
CREATE FUNCTION public.codespy_recover_embedding_dispatches(p_age integer,p_limit integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE n integer;
BEGIN
 IF p_age IS NULL OR p_age<60 OR p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
 RAISE EXCEPTION 'embedding_recovery_bounds' USING ERRCODE='23514'; END IF;
 WITH due AS (SELECT org_id,reservation_id FROM public.embedding_reservations
 WHERE state='reserved' AND dispatch_intent_at < clock_timestamp()-make_interval(secs=>p_age)
 ORDER BY dispatch_intent_at,org_id,reservation_id LIMIT p_limit FOR UPDATE SKIP LOCKED), held AS (
 UPDATE public.embedding_reservations r SET
 state='uncertain_hold',failure_reason='abandoned_dispatch',
 updated_at=clock_timestamp() FROM due WHERE r.org_id=due.org_id AND
 r.reservation_id=due.reservation_id RETURNING 1)
 SELECT count(*) INTO n FROM held; RETURN n;
END $$;
REVOKE ALL ON FUNCTION public.codespy_claim_embedding_dispatch,
 public.codespy_recover_embedding_dispatches,public.codespy_cancel_embedding_v3 FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
 GRANT EXECUTE ON FUNCTION
 public.codespy_claim_embedding_dispatch,public.codespy_recover_embedding_dispatches TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
 GRANT EXECUTE ON FUNCTION public.codespy_cancel_embedding_v3 TO p2_api;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("embedding dispatch history must be retained")
