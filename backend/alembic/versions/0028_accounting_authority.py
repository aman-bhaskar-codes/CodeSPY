"""Least-privilege financial authority routines and positive funding storage."""

from alembic import op

revision = "0028_accounting_authority"
down_revision = "0027_model_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.budget_funding_receipts (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  repository_id uuid NOT NULL,
  actor_principal_id uuid NOT NULL,
  request_key varchar(128) NOT NULL CHECK(length(request_key)>0),
  request_digest varchar(64) NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
  evidence_digest varchar(64) NOT NULL CHECK(evidence_digest ~ '^[a-f0-9]{64}$'),
  amount_micros bigint NOT NULL CHECK(amount_micros > 0),
  currency varchar(3) NOT NULL DEFAULT 'USD' CHECK(currency = 'USD'),
  reason varchar(40) NOT NULL CHECK(reason IN ('operator_initial_credit','operator_manual_credit')),
  credited_micros bigint NOT NULL CHECK(credited_micros >= 0),
  debt_repaid_micros bigint NOT NULL CHECK(debt_repaid_micros >= 0),
  resulting_balance_micros bigint NOT NULL CHECK(resulting_balance_micros >= 0),
  resulting_reserved_micros bigint NOT NULL CHECK(resulting_reserved_micros >= 0),
  resulting_debt_micros bigint NOT NULL CHECK(resulting_debt_micros >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY(org_id, id),
  CONSTRAINT uq_budget_funding_key UNIQUE(org_id, request_key),
  CONSTRAINT fk_funding_receipt_repo FOREIGN KEY(org_id, repository_id)
    REFERENCES public.repositories(org_id, id),
  CONSTRAINT fk_funding_receipt_actor FOREIGN KEY(org_id, actor_principal_id)
    REFERENCES public.memberships(org_id, principal_id)
);

CREATE FUNCTION public.codespy_guard_budget_funding() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP <> 'INSERT' THEN
  RAISE EXCEPTION 'budget funding receipt is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;

CREATE TRIGGER guard_budget_funding BEFORE INSERT OR UPDATE OR DELETE
 ON public.budget_funding_receipts FOR EACH ROW
 EXECUTE FUNCTION public.codespy_guard_budget_funding();

-- Generation reservation routine
CREATE FUNCTION public.codespy_reserve_generation(
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
  IF p_estimated_input_tokens < 0 OR p_estimated_output_tokens < 0
     OR p_reserved_micros < 0 OR p_attempt_id < 1 THEN
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

-- Commit dispatch intent routine
CREATE FUNCTION public.codespy_commit_dispatch_intent(
  p_org_id uuid,
  p_reservation_id uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_state varchar(40);
BEGIN
  SELECT state INTO v_state FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_state = 'dispatch_intent_committed' THEN
    RETURN;
  END IF;

  IF v_state <> 'reserved' THEN
    RAISE EXCEPTION 'Invalid reservation state transition' USING ERRCODE='23514';
  END IF;

  UPDATE public.model_reservations
  SET state = 'dispatch_intent_committed', updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;
END $$;

-- Mark dispatched routine
CREATE FUNCTION public.codespy_mark_dispatched(
  p_org_id uuid,
  p_reservation_id uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_state varchar(40);
BEGIN
  SELECT state INTO v_state FROM public.model_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_state = 'dispatched' THEN
    RETURN;
  END IF;

  IF v_state <> 'dispatch_intent_committed' THEN
    RAISE EXCEPTION 'Invalid reservation state transition' USING ERRCODE='23514';
  END IF;

  UPDATE public.model_reservations
  SET state = 'dispatched', updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;
END $$;

-- Settle generation routine
CREATE FUNCTION public.codespy_settle_generation(
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
      RAISE EXCEPTION
        'Reservation already settled with different provider request ID' USING ERRCODE='23514';
    END IF;
  END IF;

  IF v_res.state NOT IN ('dispatched', 'uncertain_hold') THEN
    RAISE EXCEPTION 'Cannot settle undispatched reservation' USING ERRCODE='23514';
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id
  FOR UPDATE;

  -- Match BudgetPosition.settle arithmetic
  v_available_spendable := GREATEST(
    0, (v_b.balance_micros - v_b.reserved_micros) - v_b.debt_micros
  );
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

-- Hold uncertain routine
CREATE FUNCTION public.codespy_hold_uncertain(
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

-- Release generation routine
CREATE FUNCTION public.codespy_release_generation(
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

-- Embedding reservation routine
CREATE FUNCTION public.codespy_reserve_embedding(
  p_org_id uuid,
  p_reservation_id uuid,
  p_repository_id uuid,
  p_principal_id uuid,
  p_purpose text,
  p_estimated_tokens integer,
  p_reserved_micros bigint,
  p_idempotency_key text
) RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.embedding_reservations%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
BEGIN
  SELECT * INTO v_res FROM public.embedding_reservations
  WHERE org_id = p_org_id AND idempotency_key = p_idempotency_key;

  IF FOUND THEN
    IF v_res.purpose = p_purpose
       AND v_res.estimated_tokens = p_estimated_tokens
       AND v_res.reserved_micros = p_reserved_micros THEN
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
    RAISE EXCEPTION 'Insufficient balance available for requested operation' USING ERRCODE='23514';
  END IF;

  UPDATE public.embedding_budgets
  SET reserved_micros = reserved_micros + p_reserved_micros,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = p_repository_id;

  INSERT INTO public.embedding_reservations (
    org_id, reservation_id, repository_id, principal_id, purpose,
    estimated_tokens, reserved_micros, state, idempotency_key, created_at, updated_at
  ) VALUES (
    p_org_id, p_reservation_id, p_repository_id, p_principal_id, p_purpose,
    p_estimated_tokens, p_reserved_micros, 'reserved', p_idempotency_key,
    clock_timestamp(), clock_timestamp()
  );

  RETURN p_reservation_id;
END $$;

-- Settle embedding routine
CREATE FUNCTION public.codespy_settle_embedding(
  p_org_id uuid,
  p_reservation_id uuid,
  p_actual_tokens integer,
  p_charged_micros bigint
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.embedding_reservations%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
  v_available_spendable bigint;
  v_debt_increase bigint;
  v_new_reserved bigint;
  v_unreserved_charged bigint;
  v_balance_consumed bigint;
  v_new_balance bigint;
  v_new_debt bigint;
BEGIN
  SELECT * INTO v_res FROM public.embedding_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state = 'settled' THEN
    RETURN;
  END IF;

  IF v_res.state <> 'reserved' THEN
    RAISE EXCEPTION 'Invalid reservation state transition' USING ERRCODE='23514';
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id
  FOR UPDATE;

  v_available_spendable := GREATEST(
    0, (v_b.balance_micros - v_b.reserved_micros) - v_b.debt_micros
  );
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

  UPDATE public.embedding_reservations
  SET actual_tokens = p_actual_tokens,
      charged_micros = p_charged_micros,
      state = 'settled',
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;
END $$;

-- Cancel embedding routine
CREATE FUNCTION public.codespy_cancel_embedding(
  p_org_id uuid,
  p_reservation_id uuid
) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_res public.embedding_reservations%ROWTYPE;
BEGIN
  SELECT * INTO v_res FROM public.embedding_reservations
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Reservation not found' USING ERRCODE='23514';
  END IF;

  IF v_res.state = 'cancelled' THEN
    RETURN;
  END IF;

  IF v_res.state <> 'reserved' THEN
    RAISE EXCEPTION 'Invalid reservation state transition' USING ERRCODE='23514';
  END IF;

  UPDATE public.embedding_budgets
  SET reserved_micros = reserved_micros - v_res.reserved_micros,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = v_res.repository_id;

  UPDATE public.embedding_reservations
  SET state = 'cancelled', updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND reservation_id = p_reservation_id;
END $$;

-- Funding routine
CREATE FUNCTION public.codespy_fund_budget(
  p_org_id uuid,
  p_audit_id uuid,
  p_repository_id uuid,
  p_actor_id uuid,
  p_request_key text,
  p_request_digest text,
  p_evidence_digest text,
  p_amount_micros bigint,
  p_reason text
) RETURNS public.budget_funding_receipts
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE
  v_prev public.budget_funding_receipts%ROWTYPE;
  v_b public.embedding_budgets%ROWTYPE;
  v_repayment bigint;
  v_new_debt bigint;
  v_new_balance bigint;
BEGIN
  SELECT * INTO v_prev FROM public.budget_funding_receipts
  WHERE org_id = p_org_id AND request_key = p_request_key;

  IF FOUND THEN
    IF v_prev.request_digest <> p_request_digest THEN
      RAISE EXCEPTION
        'duplicate funding request key conflicts with existing audit' USING ERRCODE='23514';
    END IF;
    RETURN v_prev;
  END IF;

  SELECT * INTO v_b FROM public.embedding_budgets
  WHERE org_id = p_org_id AND repository_id = p_repository_id
  FOR UPDATE;

  IF NOT FOUND THEN
    RAISE EXCEPTION 'Budget account does not exist for repository' USING ERRCODE='23514';
  END IF;

  v_repayment := LEAST(p_amount_micros, v_b.debt_micros);
  v_new_debt := v_b.debt_micros - v_repayment;
  v_new_balance := v_b.balance_micros + p_amount_micros - v_repayment;

  UPDATE public.embedding_budgets
  SET balance_micros = v_new_balance,
      debt_micros = v_new_debt,
      write_version = write_version + 1,
      updated_at = clock_timestamp()
  WHERE org_id = p_org_id AND repository_id = p_repository_id;

  INSERT INTO public.budget_funding_receipts (
    org_id, id, repository_id, actor_principal_id, request_key,
    request_digest, evidence_digest, amount_micros, currency,
    reason, credited_micros, debt_repaid_micros,
    resulting_balance_micros, resulting_reserved_micros, resulting_debt_micros,
    created_at
  ) VALUES (
    p_org_id, p_audit_id, p_repository_id, p_actor_id, p_request_key,
    p_request_digest, p_evidence_digest, p_amount_micros, 'USD',
    p_reason, p_amount_micros - v_repayment, v_repayment,
    v_new_balance, v_b.reserved_micros, v_new_debt,
    clock_timestamp()
  ) RETURNING * INTO v_prev;

  RETURN v_prev;
END $$;

REVOKE ALL ON FUNCTION public.codespy_reserve_generation FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_commit_dispatch_intent FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_mark_dispatched FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_settle_generation FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_hold_uncertain FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_release_generation FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_reserve_embedding FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_settle_embedding FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_cancel_embedding FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_fund_budget FROM PUBLIC;

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.embedding_budgets FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.price_schedules FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.embedding_reservations FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_reservations FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_spend_ledger FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_usage_reconciliations FROM p2_api;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.budget_funding_receipts FROM p2_api;
  GRANT SELECT ON public.embedding_budgets, public.price_schedules, public.model_reservations,
    public.model_spend_ledger, public.budget_funding_receipts,
    public.embedding_reservations TO p2_api;
  GRANT EXECUTE ON FUNCTION public.codespy_reserve_generation TO p2_api;
  GRANT EXECUTE ON FUNCTION public.codespy_release_generation TO p2_api;
  GRANT EXECUTE ON FUNCTION public.codespy_reserve_embedding TO p2_api;
  GRANT EXECUTE ON FUNCTION public.codespy_cancel_embedding TO p2_api;
 END IF;

 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.embedding_budgets FROM p2_broker;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.embedding_reservations FROM p2_broker;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_reservations FROM p2_broker;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_spend_ledger FROM p2_broker;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.model_usage_reconciliations FROM p2_broker;
  REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.budget_funding_receipts FROM p2_broker;
  GRANT SELECT ON public.embedding_budgets, public.price_schedules, public.model_reservations,
    public.model_spend_ledger, public.budget_funding_receipts,
    public.embedding_reservations TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_commit_dispatch_intent TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_mark_dispatched TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_settle_generation TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_hold_uncertain TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_settle_embedding TO p2_broker;
 END IF;

 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
  GRANT SELECT ON public.embedding_budgets, public.price_schedules, public.model_reservations,
    public.model_spend_ledger, public.budget_funding_receipts,
    public.model_usage_reconciliations TO p2_operator;
  GRANT EXECUTE ON FUNCTION public.codespy_fund_budget TO p2_operator;
  GRANT INSERT, UPDATE ON public.price_schedules TO p2_operator;
  GRANT INSERT ON public.embedding_budgets TO p2_operator;
 END IF;
END $$;
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.budget_funding_receipts) THEN
  RAISE EXCEPTION 'cannot downgrade budget funding history' USING ERRCODE='23514';
 END IF;
END $$;

DROP TRIGGER IF EXISTS guard_budget_funding ON public.budget_funding_receipts;
DROP FUNCTION IF EXISTS public.codespy_guard_budget_funding();
DROP FUNCTION IF EXISTS public.codespy_reserve_generation;
DROP FUNCTION IF EXISTS public.codespy_commit_dispatch_intent;
DROP FUNCTION IF EXISTS public.codespy_mark_dispatched;
DROP FUNCTION IF EXISTS public.codespy_settle_generation;
DROP FUNCTION IF EXISTS public.codespy_hold_uncertain;
DROP FUNCTION IF EXISTS public.codespy_release_generation;
DROP FUNCTION IF EXISTS public.codespy_reserve_embedding;
DROP FUNCTION IF EXISTS public.codespy_settle_embedding;
DROP FUNCTION IF EXISTS public.codespy_cancel_embedding;
DROP FUNCTION IF EXISTS public.codespy_fund_budget;
DROP TABLE IF EXISTS public.budget_funding_receipts;
""")
