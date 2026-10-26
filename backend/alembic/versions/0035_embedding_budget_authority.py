"""Constrained embedding accounting against the actual shared budget schema."""

from alembic import op

revision = "0035_embedding_budget_authority"
down_revision = "0034_scoped_model_result_reads"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.embedding_reservations
 ADD COLUMN request_digest text,
 ADD COLUMN price_version text,
 ADD COLUMN captured_input_rate bigint,
 ADD COLUMN actual_tokens bigint,
 ADD COLUMN outcome_digest text;
CREATE FUNCTION public.codespy_embedding_reservation_history_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='DELETE' THEN
  RAISE EXCEPTION 'embedding accounting history is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.state IN ('committed','released') AND NEW IS DISTINCT FROM OLD THEN
  RAISE EXCEPTION 'terminal embedding accounting is immutable' USING ERRCODE='23514'; END IF;
 IF (to_jsonb(NEW)-ARRAY['state','settled_micros','failure_reason','updated_at',
      'actual_tokens','outcome_digest']) IS DISTINCT FROM
    (to_jsonb(OLD)-ARRAY['state','settled_micros','failure_reason','updated_at',
      'actual_tokens','outcome_digest']) THEN
  RAISE EXCEPTION 'embedding request and captured price are immutable'
   USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_embedding_reservation_history_guard() FROM PUBLIC;
CREATE TRIGGER guard_embedding_reservation_history BEFORE UPDATE OR DELETE
 ON public.embedding_reservations FOR EACH ROW
 EXECUTE FUNCTION public.codespy_embedding_reservation_history_guard();
CREATE FUNCTION public.codespy_reserve_embedding_v2(p jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.embedding_reservations%ROWTYPE; b public.embedding_budgets%ROWTYPE;
 price public.price_schedules%ROWTYPE; oid uuid; repo uuid; actor uuid; token_count bigint;
 amount bigint; bound bigint; digest text; key text;
BEGIN
 oid:=(p->>'org_id')::uuid; repo:=(p->>'repository_id')::uuid;
 actor:=(p->>'principal_id')::uuid; key:=p->>'idempotency_key';
 IF NOT public.codespy_model_actor_write(oid,actor,repo) THEN
  RAISE EXCEPTION 'embedding_scope_denied' USING ERRCODE='42501'; END IF;
 IF key IS NULL OR octet_length(key) NOT BETWEEN 1 AND 128
  OR jsonb_typeof(p->'estimated_tokens')<>'number'
  OR (p->>'estimated_tokens')::numeric<>trunc((p->>'estimated_tokens')::numeric)
  OR jsonb_typeof(p->'max_charge_micros')<>'number'
  OR (p->>'max_charge_micros')::numeric<>trunc((p->>'max_charge_micros')::numeric) THEN
  RAISE EXCEPTION 'embedding_invalid_request' USING ERRCODE='23514'; END IF;
 token_count:=(p->>'estimated_tokens')::bigint; bound:=(p->>'max_charge_micros')::bigint;
 IF token_count NOT BETWEEN 1 AND 2147483647 OR bound<1 THEN
  RAISE EXCEPTION 'embedding_invalid_request' USING ERRCODE='23514'; END IF;
 digest:=public.codespy_scan_digest(p);
 PERFORM pg_advisory_xact_lock(hashtextextended(oid::text||':embedding:'||key,0));
 SELECT * INTO r FROM public.embedding_reservations WHERE org_id=oid AND idempotency_key=key;
 IF FOUND THEN
  IF r.request_digest IS DISTINCT FROM digest THEN
   RAISE EXCEPTION 'embedding_conflict' USING ERRCODE='23514'; END IF;
  RETURN to_jsonb(r);
 END IF;
 SELECT * INTO price FROM public.price_schedules
 WHERE provider=p->>'provider' AND model_id=p->>'model_id'
  AND effective_at<=clock_timestamp() AND expires_at>clock_timestamp()
 ORDER BY effective_at DESC,version DESC LIMIT 1;
 IF NOT FOUND THEN RAISE EXCEPTION 'embedding_price_unavailable' USING ERRCODE='23514'; END IF;
 IF ceil(token_count::numeric*price.input_micros_per_1k/1000)>bound THEN
  RAISE EXCEPTION 'embedding_budget_exceeded' USING ERRCODE='23514'; END IF;
 amount:=ceil(token_count::numeric*price.input_micros_per_1k/1000)::bigint;
 SELECT * INTO b FROM public.embedding_budgets WHERE org_id=oid AND repository_id=repo FOR UPDATE;
 IF NOT FOUND OR b.debt_micros>0 OR b.balance_micros-b.reserved_micros<amount THEN
  RAISE EXCEPTION 'embedding_budget_exceeded' USING ERRCODE='23514'; END IF;
 UPDATE public.embedding_budgets SET reserved_micros=reserved_micros+amount,
  updated_at=clock_timestamp(),write_version=write_version+1
  WHERE org_id=oid AND repository_id=repo;
 INSERT INTO public.embedding_reservations(org_id,reservation_id,repository_id,principal_id,
  provider,model_id,estimated_tokens,estimated_micros,state,idempotency_key,request_digest,
  price_version,captured_input_rate)
 VALUES(oid,gen_random_uuid(),repo,actor,p->>'provider',p->>'model_id',token_count,amount,
  'reserved',key,digest,price.version,price.input_micros_per_1k) RETURNING * INTO r;
 RETURN to_jsonb(r);
END $$;

CREATE FUNCTION public.codespy_transition_embedding_v2(oid uuid,rid uuid,p jsonb) RETURNS jsonb
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
  actual_tokens=tokens,outcome_digest=digest,failure_reason=NULL,updated_at=clock_timestamp()
 WHERE org_id=oid AND reservation_id=rid RETURNING * INTO r;
 RETURN to_jsonb(r);
END $$;
CREATE FUNCTION public.codespy_cancel_embedding_v2(oid uuid,rid uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT public.codespy_transition_embedding_v2(oid,rid,jsonb_build_object('status','cancel'))
$$;
REVOKE ALL ON FUNCTION public.codespy_reserve_embedding_v2 FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_transition_embedding_v2 FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_cancel_embedding_v2 FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT EXECUTE ON FUNCTION public.codespy_reserve_embedding_v2,
   public.codespy_cancel_embedding_v2 TO p2_api;
  REVOKE EXECUTE ON FUNCTION public.codespy_reserve_embedding,
   public.codespy_cancel_embedding FROM p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT EXECUTE ON FUNCTION public.codespy_transition_embedding_v2 TO p2_broker;
  REVOKE EXECUTE ON FUNCTION public.codespy_settle_embedding FROM p2_broker;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("captured embedding accounting authority must be retained")
