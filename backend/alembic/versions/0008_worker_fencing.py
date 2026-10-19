# ruff: noqa: E501

"""Worker-bound authority; legacy Phase 1 rows keep their original semantics."""

from alembic import op

revision = "0008_worker_fencing"
down_revision = "0007_jobs_events"
branch_labels = None
depends_on = None

SQL = r"""
ALTER TABLE public.analysis_runs ADD COLUMN worker_job_id UUID NULL;
ALTER TABLE public.analysis_runs ADD COLUMN worker_attempt_id UUID NULL;
ALTER TABLE public.analysis_runs ADD COLUMN worker_fence BIGINT NULL;
ALTER TABLE public.analysis_runs ADD CONSTRAINT ck_run_worker_binding CHECK(num_nonnulls(worker_job_id,worker_attempt_id,worker_fence) IN (0,3));
ALTER TABLE public.analysis_runs ADD CONSTRAINT fk_run_worker_attempt FOREIGN KEY(org_id,worker_job_id,worker_attempt_id,worker_fence) REFERENCES public.job_attempts(org_id,job_id,id,fence) ON DELETE RESTRICT;

CREATE FUNCTION public.codespy_phase2_authority() RETURNS UUID LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE j public.jobs%ROWTYPE; a public.job_attempts%ROWTYPE; jid uuid; aid uuid; oid uuid; f bigint;
BEGIN
 oid:=nullif(current_setting('codespy.org_id',true),'')::uuid;
 jid:=nullif(current_setting('codespy.job_id',true),'')::uuid;
 aid:=nullif(current_setting('codespy.attempt_id',true),'')::uuid;
 f:=nullif(current_setting('codespy.fence',true),'')::bigint;
 IF oid IS NULL OR jid IS NULL OR aid IS NULL OR f IS NULL THEN RAISE EXCEPTION 'worker context required' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 SELECT * INTO j FROM public.jobs WHERE org_id=nullif(current_setting('codespy.org_id',true),'')::uuid AND id=jid FOR UPDATE;
 SELECT * INTO a FROM public.job_attempts WHERE org_id=j.org_id AND job_id=jid AND id=aid FOR UPDATE;
 IF j.id IS NULL OR a.id IS NULL OR j.state<>'running' OR a.state<>'active' OR j.current_attempt_id IS DISTINCT FROM aid OR j.fence<>f OR a.fence<>f OR j.cancel_requested_at IS NOT NULL OR j.deadline<=clock_timestamp() OR a.lease_expires_at<=clock_timestamp() THEN
  RAISE EXCEPTION 'stale attempt' USING ERRCODE='23514',CONSTRAINT='phase2_fence';
 END IF;
 IF NOT EXISTS (
  SELECT 1 FROM public.memberships m JOIN public.repository_grants g
  ON g.org_id=m.org_id AND g.principal_id=m.principal_id
  WHERE m.org_id=j.org_id AND m.principal_id=j.principal_id
    AND m.role IN ('owner','admin','member') AND g.repository_id=j.repository_id
    AND g.permission='write' AND g.revoked_at IS NULL
 ) THEN RAISE EXCEPTION 'repository authority revoked' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 IF EXISTS(SELECT 1 FROM public.repositories r WHERE r.org_id=j.org_id AND r.id=j.repository_id AND r.source_kind='github') AND NOT EXISTS (
  SELECT 1 FROM public.github_repository_bindings b JOIN public.github_user_grants u
    ON u.principal_id=j.principal_id WHERE b.org_id=j.org_id AND b.repository_id=j.repository_id
    AND b.state='active' AND u.revoked_at IS NULL AND u.expires_at>clock_timestamp()
 ) THEN RAISE EXCEPTION 'provider authority revoked' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 RETURN jid;
END $$;

REVOKE ALL ON FUNCTION public.codespy_phase2_authority() FROM PUBLIC;

CREATE FUNCTION public.codespy_phase2_source_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; rid uuid;
BEGIN
 IF current_user<>'p2_broker' AND nullif(current_setting('codespy.job_id',true),'') IS NULL THEN RETURN NEW; END IF;
 jid:=public.codespy_phase2_authority(); SELECT * INTO j FROM public.jobs WHERE org_id=nullif(current_setting('codespy.org_id',true),'')::uuid AND id=jid;
 IF TG_TABLE_NAME IN ('artifacts','source_snapshots') THEN rid:=NEW.repository_id;
 ELSE SELECT repository_id INTO rid FROM public.source_snapshots WHERE org_id=NEW.org_id AND id=NEW.snapshot_id; END IF;
 IF j.operation<>'admit_source' OR NEW.org_id<>j.org_id OR rid IS DISTINCT FROM j.repository_id THEN RAISE EXCEPTION 'foreign admission source' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_phase2_run_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; a public.job_attempts%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND ROW(NEW.worker_job_id,NEW.worker_attempt_id,NEW.worker_fence) IS DISTINCT FROM ROW(OLD.worker_job_id,OLD.worker_attempt_id,OLD.worker_fence) THEN RAISE EXCEPTION 'immutable worker binding' USING ERRCODE='23514'; END IF;
 IF NEW.worker_job_id IS NULL THEN
  IF current_user='p2_broker' THEN RAISE EXCEPTION 'broker run must be bound' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF TG_OP='UPDATE' AND NEW.status IN ('failed','cancelled') AND current_setting('codespy.recovery',true)='true' THEN
  SELECT * INTO a FROM public.job_attempts WHERE org_id=NEW.org_id AND job_id=NEW.worker_job_id AND id=NEW.worker_attempt_id;
  IF a.run_id IS DISTINCT FROM NEW.id OR a.fence IS DISTINCT FROM NEW.worker_fence OR a.state NOT IN ('failed','expired','cancelled') THEN RAISE EXCEPTION 'foreign recovery run' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 jid:=public.codespy_phase2_authority();
 SELECT * INTO a FROM public.job_attempts WHERE org_id=NEW.org_id AND job_id=jid AND id=NEW.worker_attempt_id;
 IF NEW.worker_attempt_id IS DISTINCT FROM nullif(current_setting('codespy.attempt_id',true),'')::uuid OR NEW.worker_fence IS DISTINCT FROM nullif(current_setting('codespy.fence',true),'')::bigint OR NEW.org_id IS DISTINCT FROM nullif(current_setting('codespy.org_id',true),'')::uuid OR jid<>NEW.worker_job_id OR a.id IS NULL OR a.fence<>NEW.worker_fence OR a.run_id IS DISTINCT FROM NEW.id OR a.run_snapshot_id IS DISTINCT FROM NEW.snapshot_id THEN RAISE EXCEPTION 'foreign worker run' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_phase2_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE r public.analysis_runs%ROWTYPE; jid uuid;
BEGIN
 SELECT * INTO r FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.run_id FOR UPDATE;
 IF r.worker_job_id IS NULL THEN
  IF current_user='p2_broker' THEN RAISE EXCEPTION 'broker result must be bound' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 jid:=public.codespy_phase2_authority();
 IF NEW.org_id IS DISTINCT FROM nullif(current_setting('codespy.org_id',true),'')::uuid OR jid<>r.worker_job_id OR nullif(current_setting('codespy.attempt_id',true),'')::uuid<>r.worker_attempt_id OR nullif(current_setting('codespy.fence',true),'')::bigint<>r.worker_fence THEN RAISE EXCEPTION 'foreign result attempt' USING ERRCODE='23514',CONSTRAINT='phase2_fence'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_phase2_job_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; checked_at timestamptz;
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.state<>'queued' OR NEW.fence<>0 OR NEW.attempt_count<>0 OR NEW.current_attempt_id IS NOT NULL OR NEW.finished_at IS NOT NULL OR NEW.publication_checked_at IS NOT NULL THEN RAISE EXCEPTION 'job must start queued' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF OLD.state IN ('succeeded','partial','failed','cancelled') THEN
  IF (to_jsonb(OLD)-ARRAY['event_sequence','events_min_sequence']) IS DISTINCT FROM (to_jsonb(NEW)-ARRAY['event_sequence','events_min_sequence']) OR NEW.event_sequence<OLD.event_sequence OR NEW.events_min_sequence<OLD.events_min_sequence THEN
    RAISE EXCEPTION 'terminal job immutable' USING ERRCODE='23514';
  END IF;
 ELSIF NEW.state IN ('succeeded','partial') THEN
  jid:=public.codespy_phase2_authority();
  IF NEW.org_id IS DISTINCT FROM nullif(current_setting('codespy.org_id',true),'')::uuid OR jid<>NEW.id THEN RAISE EXCEPTION 'foreign publication' USING ERRCODE='23514'; END IF;
  checked_at:=clock_timestamp();
  IF EXISTS(SELECT 1 FROM public.jobs j JOIN public.job_attempts a
       ON a.org_id=j.org_id AND a.job_id=j.id AND a.id=j.current_attempt_id
       WHERE j.org_id=NEW.org_id AND j.id=NEW.id
         AND (j.deadline<=checked_at OR a.lease_expires_at<=checked_at)) THEN
    RAISE EXCEPTION 'publication deadline expired' USING ERRCODE='23514',CONSTRAINT='phase2_fence';
  END IF;
  NEW.publication_checked_at:=checked_at;
 END IF;
 RETURN NEW;
END $$;
CREATE FUNCTION public.codespy_submission_history_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF OLD.state IN ('admitted','rejected','failed','expired','cancelled') AND NEW IS DISTINCT FROM OLD THEN
   RAISE EXCEPTION 'terminal submission immutable' USING ERRCODE='23514';
 END IF;
 IF ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.principal_id,NEW.source_kind,NEW.request_key,NEW.request_hash,NEW.retry_of_submission_id,NEW.reserved_bytes,NEW.policy_hash)
    IS DISTINCT FROM ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.principal_id,OLD.source_kind,OLD.request_key,OLD.request_hash,OLD.retry_of_submission_id,OLD.reserved_bytes,OLD.policy_hash) THEN
   RAISE EXCEPTION 'submission identity immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.resolved_commit IS NOT NULL AND NEW.resolved_commit IS DISTINCT FROM OLD.resolved_commit THEN
   RAISE EXCEPTION 'selected commit immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.input_object_id IS NOT NULL AND ROW(NEW.input_object_id,NEW.actual_sha,NEW.actual_size) IS DISTINCT FROM ROW(OLD.input_object_id,OLD.actual_sha,OLD.actual_size) THEN
   RAISE EXCEPTION 'selected acquisition immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_submission_history BEFORE UPDATE ON public.source_submissions FOR EACH ROW EXECUTE FUNCTION public.codespy_submission_history_guard();
CREATE TRIGGER guard_phase2_job BEFORE INSERT OR UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.codespy_phase2_job_guard();
CREATE TRIGGER guard_phase2_run BEFORE INSERT OR UPDATE ON public.analysis_runs FOR EACH ROW EXECUTE FUNCTION public.codespy_phase2_run_guard();
"""


def upgrade() -> None:
    op.execute(SQL)
    for table in ["artifacts", "source_snapshots", "source_files", "inventory_entries"]:
        events = "INSERT OR UPDATE" if table == "source_snapshots" else "INSERT"
        op.execute(
            f"CREATE TRIGGER guard_phase2_source BEFORE {events} ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.codespy_phase2_source_guard()"
        )
    for table in ["cpg_nodes", "cpg_edges", "evidence_records", "evidence_derivations"]:
        op.execute(
            f"CREATE TRIGGER guard_phase2_result BEFORE INSERT ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.codespy_phase2_result_guard()"
        )
    op.execute(
        "CREATE TRIGGER guard_inventory_immutable BEFORE UPDATE OR DELETE ON public.inventory_entries FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER guard_submission_history ON public.source_submissions")
    op.execute("DROP FUNCTION public.codespy_submission_history_guard()")
    op.execute("DROP TRIGGER guard_inventory_immutable ON public.inventory_entries")
    for table in ["artifacts", "source_snapshots", "source_files", "inventory_entries"]:
        op.execute(f"DROP TRIGGER guard_phase2_source ON public.{table}")
    for table in ["cpg_nodes", "cpg_edges", "evidence_records", "evidence_derivations"]:
        op.execute(f"DROP TRIGGER guard_phase2_result ON public.{table}")
    op.execute("DROP TRIGGER guard_phase2_run ON public.analysis_runs")
    op.execute("DROP TRIGGER guard_phase2_job ON public.jobs")
    for name in [
        "codespy_phase2_job_guard",
        "codespy_phase2_result_guard",
        "codespy_phase2_run_guard",
        "codespy_phase2_source_guard",
        "codespy_phase2_authority",
    ]:
        op.execute(f"DROP FUNCTION public.{name}()")
    op.execute(
        "ALTER TABLE public.analysis_runs DROP CONSTRAINT fk_run_worker_attempt, DROP CONSTRAINT ck_run_worker_binding"
    )
    for name in ["worker_job_id", "worker_attempt_id", "worker_fence"]:
        op.drop_column("analysis_runs", name)
