"""Correct model charge authority and scope transport state access."""

from alembic import op

revision = "0030_model_runtime_authority"
down_revision = "0029_model_transport_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
CREATE FUNCTION public.codespy_apply_model_charge(p_org uuid,p_repo uuid,p_hold
 bigint,p_charge bigint)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE b public.embedding_budgets; remaining bigint; owed numeric; payment numeric;
BEGIN
 IF p_hold<0 OR p_charge<0 THEN RAISE EXCEPTION 'invalid charge' USING ERRCODE='23514'; END IF;
 SELECT * INTO b FROM public.embedding_budgets WHERE org_id=p_org AND repository_id=p_repo FOR
 UPDATE;
 IF NOT FOUND OR p_hold>b.reserved_micros THEN
  RAISE EXCEPTION 'invalid budget hold' USING ERRCODE='23514'; END IF;
 remaining:=b.reserved_micros-p_hold;
 owed:=b.debt_micros::numeric+p_charge::numeric;
 payment:=least(owed,b.balance_micros-remaining);
 IF owed-payment>9223372036854775807 THEN
  RAISE EXCEPTION 'budget integer overflow' USING ERRCODE='23514'; END IF;
 UPDATE public.embedding_budgets SET balance_micros=balance_micros-payment,
  reserved_micros=remaining,debt_micros=owed-payment,write_version=write_version+1,
  updated_at=clock_timestamp() WHERE org_id=p_org AND repository_id=p_repo;
END $$;
REVOKE ALL ON FUNCTION public.codespy_apply_model_charge FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.codespy_settle_generation(
 p_org_id uuid,p_reservation_id uuid,p_input_tokens integer,p_output_tokens integer,
 p_thinking_tokens integer,p_charged_micros bigint,p_provider_request_id text,
 p_actual_cache_read_tokens integer DEFAULT 0,p_actual_cache_write_tokens integer DEFAULT 0)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.model_reservations; l public.model_spend_ledger;
BEGIN
 IF least(p_input_tokens,p_output_tokens,p_thinking_tokens,p_charged_micros,
    p_actual_cache_read_tokens,p_actual_cache_write_tokens)<0 THEN
  RAISE EXCEPTION 'invalid usage' USING ERRCODE='23514'; END IF;
 SELECT * INTO r FROM public.model_reservations WHERE org_id=p_org_id
  AND reservation_id=p_reservation_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'reservation unavailable' USING ERRCODE='23514'; END IF;
 IF r.state='settled' THEN
  SELECT * INTO l FROM public.model_spend_ledger WHERE org_id=p_org_id
   AND reservation_id=p_reservation_id AND NOT is_uncertain_hold;
  IF NOT FOUND OR r.settled_micros<>p_charged_micros
    OR r.provider_request_id IS DISTINCT FROM p_provider_request_id
    OR l.input_tokens<>p_input_tokens OR l.output_tokens<>p_output_tokens
    OR l.thinking_tokens<>p_thinking_tokens
    OR l.cache_read_tokens IS DISTINCT FROM p_actual_cache_read_tokens::bigint
    OR l.cache_write_tokens IS DISTINCT FROM p_actual_cache_write_tokens::bigint THEN
   RAISE EXCEPTION 'settlement replay conflict' USING ERRCODE='23514'; END IF;
  RETURN;
 END IF;
 IF r.state NOT IN ('dispatch_intent_committed','dispatched','uncertain_hold') THEN
  RAISE EXCEPTION 'reservation has no dispatch' USING ERRCODE='23514'; END IF;
 PERFORM
 public.codespy_apply_model_charge(p_org_id,r.repository_id,r.reserved_micros,p_charged_micros);
 UPDATE public.model_reservations SET state='settled',settled_micros=p_charged_micros,
  provider_request_id=p_provider_request_id,updated_at=clock_timestamp()
  WHERE org_id=p_org_id AND reservation_id=p_reservation_id;
 INSERT INTO public.model_spend_ledger(org_id,id,repository_id,reservation_id,investigation_id,
  attempt_id,provider,model_id,charge_category,input_tokens,output_tokens,thinking_tokens,
  cache_read_tokens,cache_write_tokens,charged_micros,is_uncertain_hold)
 VALUES(p_org_id,gen_random_uuid(),r.repository_id,p_reservation_id,r.investigation_id,
  r.attempt_id,r.provider,r.model_id,r.charge_category,p_input_tokens,p_output_tokens,
  p_thinking_tokens,p_actual_cache_read_tokens,p_actual_cache_write_tokens,p_charged_micros,false);
END $$;

CREATE OR REPLACE FUNCTION public.codespy_release_generation(
 p_org_id uuid,p_reservation_id uuid,p_failure_reason text)
 RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.model_reservations;
BEGIN
 SELECT * INTO r FROM public.model_reservations WHERE org_id=p_org_id
  AND reservation_id=p_reservation_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'reservation unavailable' USING ERRCODE='23514'; END IF;
 IF r.state='rejected_before_dispatch' THEN RETURN; END IF;
 IF r.state<>'reserved' THEN RAISE EXCEPTION 'cannot release possible spend' USING
 ERRCODE='23514'; END IF;
 PERFORM public.codespy_apply_model_charge(p_org_id,r.repository_id,r.reserved_micros,0);
 UPDATE public.model_reservations SET
 state='rejected_before_dispatch',failure_reason=p_failure_reason,
  updated_at=clock_timestamp() WHERE org_id=p_org_id AND reservation_id=p_reservation_id;
END $$;

CREATE OR REPLACE FUNCTION public.codespy_hold_uncertain(
  p_org_id uuid,
  p_reservation_id uuid,
  p_failure_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
BEGIN
  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state IN ('uncertain_hold','settled','rejected_before_dispatch') THEN
    RETURN;
  END IF;

  IF v_res.state NOT IN ('dispatched', 'dispatch_intent_committed') THEN
    RAISE EXCEPTION 'Cannot mark uncommitted reservation as uncertain' USING ERRCODE='23514';
  END IF;

  UPDATE public.model_reservations
  SET state = 'uncertain_hold',
      failure_reason = p_failure_reason,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;

  INSERT INTO public.model_spend_ledger (
    org_id, id, repository_id, reservation_id, investigation_id,
    attempt_id, provider, model_id, charge_category,
    input_tokens, output_tokens, thinking_tokens,
    cache_read_tokens, cache_write_tokens,
    charged_micros, is_uncertain_hold, created_at
  ) VALUES (
    p_org_id, gen_random_uuid(), v_res.repository_id, p_reservation_id, v_res.investigation_id,
    v_res.attempt_id, v_res.provider, v_res.model_id, v_res.charge_category,
    v_res.estimated_input_tokens, v_res.estimated_output_tokens, 0,
    NULL, NULL,
    v_res.reserved_micros, true, clock_timestamp()
  );
END $$;


CREATE FUNCTION public.codespy_claim_model_dispatch(p_org uuid,p_res uuid) RETURNS text
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r public.model_reservations;
BEGIN
 SELECT * INTO r FROM public.model_reservations WHERE org_id=p_org AND reservation_id=p_res
 FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'reservation unavailable' USING ERRCODE='23514'; END IF;
 IF r.state<>'reserved' THEN RETURN r.state; END IF;
 IF r.price_snapshot IS NULL OR r.request_digest IS NULL THEN
  RAISE EXCEPTION 'captured model identity required' USING ERRCODE='23514'; END IF;
 PERFORM public.codespy_commit_dispatch_intent(p_org,p_res);
 PERFORM public.codespy_mark_dispatched(p_org,p_res);
 RETURN 'claimed';
END $$;

CREATE FUNCTION public.codespy_record_model_usage(p_org uuid,p_repo uuid,p_res uuid,
 p_profile text,p_digest text,p_evidence jsonb) RETURNS uuid
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE identity uuid;
BEGIN
 IF public.codespy_scan_digest(p_evidence)<>p_digest THEN
  RAISE EXCEPTION 'usage evidence digest mismatch' USING ERRCODE='23514'; END IF;
 INSERT INTO public.provider_usage_receipts(org_id,id,repository_id,reservation_id,
  profile_digest,payload_digest,quality,evidence_json)
 VALUES(p_org,gen_random_uuid(),p_repo,p_res,p_profile,p_digest,p_evidence->>'quality',p_evidence)
 ON CONFLICT(org_id,reservation_id,payload_digest) DO NOTHING RETURNING id INTO identity;
 IF identity IS NULL THEN SELECT id INTO identity FROM public.provider_usage_receipts
  WHERE org_id=p_org AND reservation_id=p_res AND payload_digest=p_digest; END IF;
 RETURN identity;
END $$;

CREATE FUNCTION public.codespy_model_actor_write(p_org uuid,p_actor uuid,p_repo uuid) RETURNS
 boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM public.memberships m JOIN public.repository_grants g
  ON g.org_id=m.org_id AND g.principal_id=m.principal_id
  WHERE m.org_id=p_org AND m.principal_id=p_actor AND m.role<>'viewer'
   AND g.repository_id=p_repo AND g.permission='write' AND g.revoked_at IS NULL)
$$;
REVOKE ALL ON FUNCTION public.codespy_model_actor_write FROM PUBLIC;

CREATE FUNCTION public.codespy_put_model_continuation(p_org uuid,p_id uuid,p_repo uuid,p_res uuid,
 p_inv uuid,p_actor uuid,p_profile text,p_cipher bytea,p_expires timestamptz) RETURNS void
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NOT public.codespy_model_actor_write(p_org,p_actor,p_repo) THEN
  RAISE EXCEPTION 'continuation unavailable' USING ERRCODE='42501'; END IF;
 INSERT INTO public.model_continuations(org_id,id,repository_id,reservation_id,
  investigation_id,principal_id,profile_digest,ciphertext,expires_at)
 VALUES(p_org,p_id,p_repo,p_res,p_inv,p_actor,p_profile,p_cipher,p_expires);
END $$;

CREATE FUNCTION public.codespy_get_model_continuation(p_org uuid,p_repo uuid,p_inv uuid,
 p_actor uuid,p_id uuid,p_profile text) RETURNS TABLE(ciphertext bytea,expires_at timestamptz)
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT c.ciphertext,c.expires_at FROM public.model_continuations c
 WHERE c.org_id=p_org AND c.repository_id=p_repo AND c.investigation_id=p_inv
  AND c.principal_id=p_actor AND c.id=p_id AND c.profile_digest=p_profile
  AND c.expires_at>clock_timestamp() AND public.codespy_model_actor_write(p_org,p_actor,p_repo)
$$;

CREATE FUNCTION public.codespy_expire_model_continuations(p_limit integer DEFAULT 100) RETURNS
 integer
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE total integer;
BEGIN
 IF p_limit<1 OR p_limit>100 THEN RAISE EXCEPTION 'invalid retention bound' USING
 ERRCODE='23514'; END IF;
 WITH expired AS(SELECT org_id,id FROM public.model_continuations WHERE
 expires_at<=clock_timestamp()
  ORDER BY expires_at,org_id,id LIMIT p_limit FOR UPDATE SKIP LOCKED)
 DELETE FROM public.model_continuations c USING expired e WHERE c.org_id=e.org_id AND c.id=e.id;
 GET DIAGNOSTICS total=ROW_COUNT;
 RETURN total;
END $$;
REVOKE ALL ON FUNCTION public.codespy_claim_model_dispatch FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_record_model_usage FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_put_model_continuation FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_get_model_continuation FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_expire_model_continuations FROM PUBLIC;
DO $$ DECLARE role_name text; BEGIN
 FOREACH role_name IN ARRAY ARRAY['p2_api','p2_broker'] LOOP
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=role_name) THEN
   EXECUTE
 format('REVOKE ALL ON public.model_continuations,'
    'public.provider_usage_receipts FROM %I',role_name);
   EXECUTE
 format('REVOKE INSERT,UPDATE,DELETE,TRUNCATE ON public.embedding_budgets,'
    'public.price_schedules,public.embedding_reservations,public.model_reservations,'
    'public.model_spend_ledger,public.model_usage_reconciliations,'
    'public.budget_funding_receipts FROM %I',role_name);
  END IF;
 END LOOP;
END $$;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT EXECUTE ON FUNCTION public.codespy_claim_model_dispatch,public.codespy_record_model_usage,
   public.codespy_put_model_continuation,public.codespy_get_model_continuation,
   public.codespy_expire_model_continuations TO p2_broker;
 END IF;
END $$;

CREATE OR REPLACE FUNCTION public.codespy_reserve_generation(
  p_org_id uuid,
  p_reservation_id uuid,
  p_repository_id uuid,
  p_principal_id uuid,
  p_investigation_id uuid,
  p_logical_call_id uuid,
  p_attempt_id integer,
  p_provider text,
  p_model_id text,
  p_charge_category text,
  p_estimated_input_tokens integer,
  p_estimated_output_tokens integer,
  p_reserved_micros bigint,
  p_idempotency_key text,
  p_request_digest text,
  p_price_snapshot jsonb
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
BEGIN
  PERFORM pg_advisory_xact_lock(hashtextextended(p_org_id::text||':'||p_idempotency_key,0));
  IF p_estimated_input_tokens < 0 OR p_estimated_output_tokens < 0 OR p_reserved_micros < 0 OR
 p_attempt_id < 1 THEN
    RAISE EXCEPTION 'Invalid reservation bounds' USING ERRCODE='23514';
  END IF;

  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND idempotency_key = p_idempotency_key;

  IF FOUND THEN
    IF v_res.repository_id = p_repository_id
       AND v_res.principal_id = p_principal_id
       AND v_res.investigation_id = p_investigation_id
       AND v_res.logical_call_id = p_logical_call_id
       AND v_res.attempt_id = p_attempt_id
       AND v_res.provider = p_provider
       AND v_res.model_id = p_model_id
       AND v_res.charge_category = p_charge_category
       AND v_res.estimated_input_tokens = p_estimated_input_tokens
       AND v_res.estimated_output_tokens = p_estimated_output_tokens
       AND v_res.reserved_micros = p_reserved_micros
       AND v_res.request_digest IS NOT DISTINCT FROM p_request_digest
       AND v_res.price_snapshot IS NOT DISTINCT FROM p_price_snapshot THEN
      RETURN v_res.reservation_id;
    ELSE
      RAISE EXCEPTION 'Reservation request key conflicts' USING ERRCODE='23514';
    END IF;
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = p_repository_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Budget account does not exist for repository' USING ERRCODE='23514';
  END IF;

  IF v_b.debt_micros > 0 OR (v_b.balance_micros - v_b.reserved_micros) < p_reserved_micros THEN
    RAISE EXCEPTION 'Insufficient spendable balance or active debt' USING ERRCODE='23514';
  END IF;

  UPDATE public.embedding_budgets
  SET reserved_micros = reserved_micros + p_reserved_micros,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = p_repository_id;

  INSERT INTO public.model_reservations (
    org_id, reservation_id, repository_id, principal_id, investigation_id,
    logical_call_id, attempt_id, provider, model_id, charge_category,
    estimated_input_tokens, estimated_output_tokens, reserved_micros,
    state, idempotency_key, request_digest, price_snapshot,
    created_at, updated_at
  ) VALUES (
    p_org_id, p_reservation_id, p_repository_id, p_principal_id, p_investigation_id,
    p_logical_call_id, p_attempt_id, p_provider, p_model_id, p_charge_category,
    p_estimated_input_tokens, p_estimated_output_tokens, p_reserved_micros,
    'reserved', p_idempotency_key, p_request_digest, p_price_snapshot,
    clock_timestamp(), clock_timestamp()
  );

  RETURN p_reservation_id;
END $$;


""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.provider_usage_receipts)
 OR EXISTS(SELECT 1 FROM public.model_continuations) THEN
  RAISE EXCEPTION 'cannot erase model runtime history' USING ERRCODE='23514'; END IF;
END $$;
CREATE OR REPLACE FUNCTION public.codespy_settle_generation(
  p_org_id uuid,
  p_reservation_id uuid,
  p_input_tokens integer,
  p_output_tokens integer,
  p_thinking_tokens integer,
  p_charged_micros bigint,
  p_provider_request_id text,
  p_actual_cache_read_tokens integer DEFAULT 0,
  p_actual_cache_write_tokens integer DEFAULT 0
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
  v_available_spendable bigint;
  v_debt_increase bigint;
  v_new_reserved bigint;
  v_unreserved_charged bigint;
  v_balance_consumed bigint;
  v_new_balance bigint;
  v_new_debt bigint;
BEGIN
  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state = 'settled' THEN
    IF v_res.provider_request_id = p_provider_request_id THEN
      RETURN;
    ELSE
      RAISE EXCEPTION 'Reservation already settled with different provider request ID' USING
 ERRCODE='23514';
    END IF;
  END IF;

  IF v_res.state NOT IN ('dispatched', 'uncertain_hold') THEN
    RAISE EXCEPTION 'Cannot settle undispatched reservation' USING ERRCODE='23514';
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id
  FOR UPDATE;

  -- Match BudgetPosition.settle arithmetic
  v_available_spendable := GREATEST(0, (v_b.balance_micros - v_b.reserved_micros) -
 v_b.debt_micros);
  v_debt_increase := GREATEST(0, p_charged_micros - v_res.reserved_micros - v_available_spendable);
  v_new_reserved := v_b.reserved_micros - v_res.reserved_micros;
  v_unreserved_charged := GREATEST(0, p_charged_micros - v_res.reserved_micros);
  v_balance_consumed := LEAST(v_b.balance_micros, v_unreserved_charged);
  v_new_balance := GREATEST(0, v_b.balance_micros - v_balance_consumed);
  v_new_debt := v_b.debt_micros + v_debt_increase;

  UPDATE public.embedding_budgets
  SET balance_micros = v_new_balance,
      reserved_micros = v_new_reserved,
      debt_micros = v_new_debt,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id;

  UPDATE public.model_reservations
  SET state = 'settled',
      settled_micros = p_charged_micros,
      provider_request_id = p_provider_request_id,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;

  INSERT INTO public.model_spend_ledger (
    org_id, id, repository_id, reservation_id, investigation_id,
    attempt_id, provider, model_id, charge_category,
    input_tokens, output_tokens, thinking_tokens,
    cache_read_tokens, cache_write_tokens,
    charged_micros, is_uncertain_hold, created_at
  ) VALUES (
    p_org_id, gen_random_uuid(), v_res.repository_id, p_reservation_id, v_res.investigation_id,
    v_res.attempt_id, v_res.provider, v_res.model_id, v_res.charge_category,
    p_input_tokens, p_output_tokens, p_thinking_tokens,
    p_actual_cache_read_tokens, p_actual_cache_write_tokens,
    p_charged_micros, false, clock_timestamp()
  );
END $$;


CREATE OR REPLACE FUNCTION public.codespy_release_generation(
  p_org_id uuid,
  p_reservation_id uuid,
  p_failure_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
BEGIN
  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state IN ('settled', 'rejected_before_dispatch') THEN
    RETURN;
  END IF;

  IF v_res.state <> 'reserved' THEN
    RAISE EXCEPTION 'Cannot release potentially spent capacity' USING ERRCODE='23514';
  END IF;

  UPDATE public.embedding_budgets
  SET reserved_micros = reserved_micros - v_res.reserved_micros,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id;

  UPDATE public.model_reservations
  SET state = 'rejected_before_dispatch',
      failure_reason = p_failure_reason,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;
END $$;


CREATE OR REPLACE FUNCTION public.codespy_reserve_generation(
  p_org_id uuid,
  p_reservation_id uuid,
  p_repository_id uuid,
  p_principal_id uuid,
  p_investigation_id uuid,
  p_logical_call_id uuid,
  p_attempt_id integer,
  p_provider text,
  p_model_id text,
  p_charge_category text,
  p_estimated_input_tokens integer,
  p_estimated_output_tokens integer,
  p_reserved_micros bigint,
  p_idempotency_key text,
  p_request_digest text,
  p_price_snapshot jsonb
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
BEGIN
  IF p_estimated_input_tokens < 0 OR p_estimated_output_tokens < 0 OR p_reserved_micros < 0 OR
 p_attempt_id < 1 THEN
    RAISE EXCEPTION 'Invalid reservation bounds' USING ERRCODE='23514';
  END IF;

  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND idempotency_key = p_idempotency_key;

  IF FOUND THEN
    IF v_res.repository_id = p_repository_id
       AND v_res.principal_id = p_principal_id
       AND v_res.investigation_id = p_investigation_id
       AND v_res.logical_call_id = p_logical_call_id
       AND v_res.attempt_id = p_attempt_id
       AND v_res.provider = p_provider
       AND v_res.model_id = p_model_id
       AND v_res.charge_category = p_charge_category
       AND v_res.estimated_input_tokens = p_estimated_input_tokens
       AND v_res.estimated_output_tokens = p_estimated_output_tokens
       AND v_res.reserved_micros = p_reserved_micros
       AND v_res.request_digest IS NOT DISTINCT FROM p_request_digest
       AND v_res.price_snapshot IS NOT DISTINCT FROM p_price_snapshot THEN
      RETURN v_res.reservation_id;
    ELSE
      RAISE EXCEPTION 'Reservation request key conflicts' USING ERRCODE='23514';
    END IF;
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = p_repository_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Budget account does not exist for repository' USING ERRCODE='23514';
  END IF;

  IF v_b.debt_micros > 0 OR (v_b.balance_micros - v_b.reserved_micros) < p_reserved_micros THEN
    RAISE EXCEPTION 'Insufficient spendable balance or active debt' USING ERRCODE='23514';
  END IF;

  UPDATE public.embedding_budgets
  SET reserved_micros = reserved_micros + p_reserved_micros,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = p_repository_id;

  INSERT INTO public.model_reservations (
    org_id, reservation_id, repository_id, principal_id, investigation_id,
    logical_call_id, attempt_id, provider, model_id, charge_category,
    estimated_input_tokens, estimated_output_tokens, reserved_micros,
    state, idempotency_key, request_digest, price_snapshot,
    created_at, updated_at
  ) VALUES (
    p_org_id, p_reservation_id, p_repository_id, p_principal_id, p_investigation_id,
    p_logical_call_id, p_attempt_id, p_provider, p_model_id, p_charge_category,
    p_estimated_input_tokens, p_estimated_output_tokens, p_reserved_micros,
    'reserved', p_idempotency_key, p_request_digest, p_price_snapshot,
    clock_timestamp(), clock_timestamp()
  );

  RETURN p_reservation_id;
END $$;



CREATE OR REPLACE FUNCTION public.codespy_hold_uncertain(
  p_org_id uuid,
  p_reservation_id uuid,
  p_failure_reason text
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.model_reservations%ROWTYPE;
BEGIN
  SELECT * INTO v_res FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state = 'uncertain_hold' THEN
    RETURN;
  END IF;

  IF v_res.state NOT IN ('dispatched', 'dispatch_intent_committed') THEN
    RAISE EXCEPTION 'Cannot mark uncommitted reservation as uncertain' USING ERRCODE='23514';
  END IF;

  UPDATE public.model_reservations
  SET state = 'uncertain_hold',
      failure_reason = p_failure_reason,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;

  INSERT INTO public.model_spend_ledger (
    org_id, id, repository_id, reservation_id, investigation_id,
    attempt_id, provider, model_id, charge_category,
    input_tokens, output_tokens, thinking_tokens,
    cache_read_tokens, cache_write_tokens,
    charged_micros, is_uncertain_hold, created_at
  ) VALUES (
    p_org_id, gen_random_uuid(), v_res.repository_id, p_reservation_id, v_res.investigation_id,
    v_res.attempt_id, v_res.provider, v_res.model_id, v_res.charge_category,
    v_res.estimated_input_tokens, v_res.estimated_output_tokens, 0,
    0, 0,
    v_res.reserved_micros, true, clock_timestamp()
  );
END $$;


DROP FUNCTION public.codespy_claim_model_dispatch;
DROP FUNCTION public.codespy_record_model_usage;
DROP FUNCTION public.codespy_put_model_continuation;
DROP FUNCTION public.codespy_get_model_continuation;
DROP FUNCTION public.codespy_expire_model_continuations;
DROP FUNCTION public.codespy_model_actor_write;
DROP FUNCTION public.codespy_apply_model_charge;
""")
