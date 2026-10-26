"""Expose encrypted model results through exact scoped reads, not table access."""

from alembic import op

revision = "0034_scoped_model_result_reads"
down_revision = "0033_investigation_publication_guards"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
CREATE FUNCTION public.codespy_get_model_result(
 p_org uuid,p_repo uuid,p_actor uuid,p_inv uuid,p_res uuid,p_call uuid,
 p_request text,p_profile text) RETURNS SETOF public.model_call_results
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT c.* FROM public.model_call_results c
 JOIN public.model_reservations r ON r.org_id=c.org_id AND r.reservation_id=c.reservation_id
 JOIN public.investigations i ON i.org_id=r.org_id AND i.id=r.investigation_id
 WHERE c.org_id=p_org AND r.repository_id=p_repo AND r.principal_id=p_actor
  AND i.principal_id=p_actor AND i.repository_id=p_repo AND i.id=p_inv
  AND c.reservation_id=p_res AND c.logical_call_id=p_call AND r.logical_call_id=p_call
  AND c.request_digest=p_request AND r.request_digest=public.codespy_scan_digest(
   jsonb_build_object('request_digest',p_request,'profile_digest',p_profile))
  AND c.profile_revision=p_profile AND c.expires_at>clock_timestamp()
  AND public.codespy_model_actor_write(p_org,p_actor,p_repo)
$$;
CREATE FUNCTION public.codespy_model_result_identity(
 p_org uuid,p_repo uuid,p_actor uuid,p_inv uuid,p_res uuid,p_call uuid,
 p_request text,p_profile text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT c.id FROM public.model_call_results c
 JOIN public.model_reservations r ON r.org_id=c.org_id AND r.reservation_id=c.reservation_id
 WHERE c.org_id=p_org AND r.repository_id=p_repo AND r.principal_id=p_actor
  AND r.investigation_id=p_inv AND c.reservation_id=p_res
  AND c.logical_call_id=p_call AND r.logical_call_id=p_call
  AND c.request_digest=p_request AND r.request_digest=public.codespy_scan_digest(
   jsonb_build_object('request_digest',p_request,'profile_digest',p_profile))
  AND c.profile_revision=p_profile
$$;
CREATE FUNCTION public.codespy_expire_model_results(p_limit integer DEFAULT 100) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE removed integer;
BEGIN
 IF p_limit<1 OR p_limit>100 THEN
  RAISE EXCEPTION 'invalid result retention limit' USING ERRCODE='23514';
 END IF;
 WITH expired AS (
  SELECT org_id,id FROM public.model_call_results WHERE expires_at<=clock_timestamp()
  ORDER BY expires_at,org_id,id LIMIT p_limit FOR UPDATE SKIP LOCKED
 )
 DELETE FROM public.model_call_results c USING expired e
  WHERE c.org_id=e.org_id AND c.id=e.id;
 GET DIAGNOSTICS removed=ROW_COUNT;
 RETURN removed;
END $$;
REVOKE ALL ON FUNCTION public.codespy_expire_model_results FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_get_model_result FROM PUBLIC;
REVOKE ALL ON FUNCTION public.codespy_model_result_identity FROM PUBLIC;
CREATE FUNCTION public.codespy_investigation_worker_projection_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
DECLARE j public.jobs%ROWTYPE;
BEGIN
 IF current_user<>'p2_broker' OR NEW.spec_hash IS NULL THEN RETURN NEW; END IF;
 IF NEW.current_job_id IS DISTINCT FROM OLD.current_job_id THEN
  RAISE EXCEPTION 'worker cannot replace investigation job' USING ERRCODE='23514';
 END IF;
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=NEW.current_job_id FOR UPDATE;
 IF j.id IS NULL OR j.operation<>'investigate_snapshot' OR j.investigation_id<>NEW.id
    OR j.spec_hash IS DISTINCT FROM NEW.spec_hash THEN
  RAISE EXCEPTION 'investigation projection scope differs' USING ERRCODE='23514';
 END IF;
 IF NEW.status='running' THEN
  IF public.codespy_phase2_authority()<>j.id THEN
   RAISE EXCEPTION 'investigation worker authority differs' USING ERRCODE='23514';
  END IF;
 ELSIF NEW.status='checkpointed' AND j.state<>'retry_wait' THEN
  RAISE EXCEPTION 'investigation retry projection differs' USING ERRCODE='23514';
 ELSIF NEW.status='cancelled' AND j.state<>'cancelled' THEN
  RAISE EXCEPTION 'investigation cancel projection differs' USING ERRCODE='23514';
 ELSIF NEW.status IN ('failed','awaiting_human_decision') AND j.state<>'failed' THEN
  RAISE EXCEPTION 'investigation failure projection differs' USING ERRCODE='23514';
 ELSIF NEW.status='completed' AND j.state NOT IN ('succeeded','partial') THEN
  RAISE EXCEPTION 'investigation completion projection differs' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_investigation_worker_projection_guard() FROM PUBLIC;
CREATE TRIGGER guard_investigation_worker_projection BEFORE UPDATE ON public.investigations
 FOR EACH ROW EXECUTE FUNCTION public.codespy_investigation_worker_projection_guard();
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  REVOKE SELECT ON public.model_call_results FROM p2_broker;
  GRANT UPDATE(current_job_id,worker_id,worker_lease_expires_at)
   ON public.investigations TO p2_broker;
  GRANT EXECUTE ON FUNCTION public.codespy_get_model_result,
   public.codespy_model_result_identity,public.codespy_expire_model_results TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  REVOKE SELECT ON public.model_call_results FROM p2_api;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    # Application rollback keeps the forward security boundary and stored history.
    raise RuntimeError("scoped ciphertext authority must be retained")
