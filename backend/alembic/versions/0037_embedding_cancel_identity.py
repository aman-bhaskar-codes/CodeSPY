"""Bind explicit cancellation reason without rewriting prior accounting revisions."""

from alembic import op

revision = "0037_embedding_cancel_identity"
down_revision = "0036_report_memory_outbox"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.codespy_transition_embedding_v2(
  oid uuid,rid uuid,p jsonb
) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.embedding_reservations%ROWTYPE; b public.embedding_budgets%ROWTYPE;
 target text; charge bigint:=0; tokens bigint; remaining bigint; owed numeric; payment bigint;
 digest text;
BEGIN
 SELECT * INTO r FROM public.embedding_reservations
 WHERE org_id=oid AND reservation_id=rid FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'embedding_not_found' USING ERRCODE='23514'; END IF;
 digest:=public.codespy_scan_digest(p);
 IF r.state='committed' OR r.state='released' THEN
  IF r.outcome_digest=digest THEN RETURN to_jsonb(r); END IF;
  RAISE EXCEPTION 'embedding_conflict' USING ERRCODE='23514';
 END IF;
 IF p->>'status'='cancel' THEN
  IF r.state<>'reserved' OR NOT public.codespy_model_actor_write(
   r.org_id,r.principal_id,r.repository_id) THEN
   RAISE EXCEPTION 'embedding_transition_denied' USING ERRCODE='23514'; END IF;
  IF jsonb_typeof(p->'reason')<>'string' OR octet_length(p->>'reason')>256 THEN
   RAISE EXCEPTION 'embedding_invalid_cancel_reason' USING ERRCODE='23514'; END IF;
  target:='released';
 ELSIF p->>'status' IN ('timeout','failed') THEN
  -- A generic failure is not proof that an external request was unspent.
  UPDATE public.embedding_reservations SET state='uncertain_hold',
   failure_reason='embedding_outcome_unknown',updated_at=clock_timestamp()
   WHERE org_id=oid AND reservation_id=rid RETURNING * INTO r;
  RETURN to_jsonb(r);
 ELSIF p->>'status'='success' THEN
  IF r.captured_input_rate IS NULL OR r.request_digest IS NULL THEN
   RAISE EXCEPTION 'embedding_legacy_requires_review' USING ERRCODE='23514'; END IF;
  IF jsonb_typeof(p->'actual_tokens')<>'number'
   OR (p->>'actual_tokens')::numeric<>trunc((p->>'actual_tokens')::numeric)
   OR jsonb_typeof(p->'actual_charge_micros')<>'number'
   OR (p->>'actual_charge_micros')::numeric<>trunc((p->>'actual_charge_micros')::numeric) THEN
   RAISE EXCEPTION 'embedding_invalid_usage' USING ERRCODE='23514'; END IF;
  tokens:=(p->>'actual_tokens')::bigint; charge:=(p->>'actual_charge_micros')::bigint;
  IF tokens<0 OR charge<0 OR charge<>ceil(tokens::numeric*r.captured_input_rate/1000) THEN
   RAISE EXCEPTION 'embedding_conflict' USING ERRCODE='23514'; END IF;
  target:='committed';
 ELSE RAISE EXCEPTION 'embedding_invalid_transition' USING ERRCODE='23514';
 END IF;
 SELECT * INTO b FROM public.embedding_budgets
 WHERE org_id=oid AND repository_id=r.repository_id FOR UPDATE;
 IF NOT FOUND OR b.reserved_micros<r.estimated_micros THEN
  RAISE EXCEPTION 'embedding_position_invalid' USING ERRCODE='23514'; END IF;
 remaining:=b.reserved_micros-r.estimated_micros; owed:=b.debt_micros::numeric+charge;
 payment:=least(owed,b.balance_micros-remaining)::bigint;
 UPDATE public.embedding_budgets SET reserved_micros=remaining,
  balance_micros=balance_micros-payment,
  debt_micros=(owed-payment)::bigint,updated_at=clock_timestamp(),write_version=write_version+1
 WHERE org_id=oid AND repository_id=r.repository_id;
 UPDATE public.embedding_reservations SET state=target,
  settled_micros=CASE WHEN target='committed' THEN charge ELSE NULL END,
  actual_tokens=tokens,outcome_digest=digest,
  failure_reason=CASE WHEN target='released' THEN p->>'reason' ELSE NULL END,
  updated_at=clock_timestamp()
 WHERE org_id=oid AND reservation_id=rid RETURNING * INTO r;
 RETURN to_jsonb(r);
END $$;
CREATE FUNCTION public.codespy_cancel_embedding_v3(oid uuid,rid uuid,p_reason text) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT public.codespy_transition_embedding_v2(oid,rid,
  jsonb_build_object('status','cancel','reason',p_reason))
$$;
REVOKE ALL ON FUNCTION public.codespy_cancel_embedding_v3 FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  REVOKE EXECUTE ON FUNCTION public.codespy_cancel_embedding_v2 FROM p2_api;
  GRANT EXECUTE ON FUNCTION public.codespy_cancel_embedding_v3 TO p2_api;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("embedding cancellation history must be retained")
