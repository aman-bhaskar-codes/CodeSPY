# ruff: noqa: E501 -- historical SQL is kept verbatim for review.
"""Serialize source/result publication and reject mutable historical records."""

from alembic import op

revision = "0004_guards"
down_revision = "0003_results"
branch_labels = None
depends_on = None

SQL = r"""
CREATE FUNCTION public.codespy_reject_mutation() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN RAISE EXCEPTION 'immutable record' USING ERRCODE='23514', CONSTRAINT='guard_immutable'; END $$;

CREATE FUNCTION public.codespy_snapshot_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='INSERT' THEN
   IF NEW.state<>'building' THEN RAISE EXCEPTION 'snapshot must start building' USING ERRCODE='23514'; END IF;
 ELSE
   IF OLD.state<>'building' OR NEW.state<>'sealed' OR
      (to_jsonb(NEW)-ARRAY['state','manifest_hash','sealed_at']) IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['state','manifest_hash','sealed_at']) THEN
     RAISE EXCEPTION 'immutable snapshot identity' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_snapshot BEFORE INSERT OR UPDATE ON public.source_snapshots FOR EACH ROW EXECUTE FUNCTION public.codespy_snapshot_guard();

CREATE FUNCTION public.codespy_file_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE parent_state text;
BEGIN
 SELECT state INTO parent_state FROM public.source_snapshots WHERE org_id=NEW.org_id AND id=NEW.snapshot_id FOR UPDATE;
 IF parent_state IS DISTINCT FROM 'building' THEN RAISE EXCEPTION 'snapshot not building' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_file_insert BEFORE INSERT ON public.source_files FOR EACH ROW EXECUTE FUNCTION public.codespy_file_guard();

CREATE FUNCTION public.codespy_run_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE parent_state text;
BEGIN
 IF TG_OP='INSERT' THEN
   SELECT state INTO parent_state FROM public.source_snapshots WHERE org_id=NEW.org_id AND id=NEW.snapshot_id FOR UPDATE;
   IF parent_state IS DISTINCT FROM 'sealed' OR NEW.status<>'queued' OR NEW.write_version<>0 OR
      NEW.started_at IS NOT NULL OR NEW.finished_at IS NOT NULL OR NEW.result_hash IS NOT NULL OR NEW.failure_code IS NOT NULL THEN
     RAISE EXCEPTION 'run requires sealed source and queued initial state' USING ERRCODE='23514';
   END IF;
 ELSE
   IF NEW.write_version<>OLD.write_version+1 OR NOT
      ((OLD.status='queued' AND NEW.status IN ('running','cancelled')) OR
       (OLD.status='running' AND NEW.status IN ('completed','partial','failed','cancelled'))) THEN
     RAISE EXCEPTION 'invalid lifecycle transition' USING ERRCODE='23514';
   END IF;
   IF (to_jsonb(NEW)-ARRAY['status','write_version','started_at','finished_at','coverage','limitations','result_hash','failure_code']) IS DISTINCT FROM
      (to_jsonb(OLD)-ARRAY['status','write_version','started_at','finished_at','coverage','limitations','result_hash','failure_code']) THEN
     RAISE EXCEPTION 'immutable run identity' USING ERRCODE='23514';
   END IF;
   IF OLD.status='running' AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
     RAISE EXCEPTION 'immutable start time' USING ERRCODE='23514';
   END IF;
   IF NEW.status IN ('cancelled','failed') AND
      (NEW.started_at IS DISTINCT FROM OLD.started_at OR
       NEW.coverage IS DISTINCT FROM OLD.coverage OR
       NEW.limitations IS DISTINCT FROM OLD.limitations) THEN
     RAISE EXCEPTION 'invalid terminal transition fields' USING ERRCODE='23514';
   END IF;
   IF NEW.status='running' AND (NEW.started_at IS NULL OR NEW.coverage IS DISTINCT FROM OLD.coverage OR NEW.limitations IS DISTINCT FROM OLD.limitations) THEN
     RAISE EXCEPTION 'invalid start fields' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_run BEFORE INSERT OR UPDATE ON public.analysis_runs FOR EACH ROW EXECUTE FUNCTION public.codespy_run_guard();

CREATE FUNCTION public.codespy_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE parent_state text; file_size bigint;
BEGIN
 SELECT status INTO parent_state FROM public.analysis_runs WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.run_id FOR UPDATE;
 IF parent_state IS DISTINCT FROM 'running' THEN RAISE EXCEPTION 'run not running' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME IN ('cpg_nodes','evidence_records') THEN
   IF NEW.file_id IS NOT NULL AND NEW.end_byte IS NOT NULL THEN
     SELECT size_bytes INTO file_size FROM public.source_files WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id AND id=NEW.file_id;
     IF file_size IS NULL OR NEW.end_byte>file_size THEN RAISE EXCEPTION 'range exceeds file' USING ERRCODE='23514'; END IF;
   END IF;
 END IF;
 RETURN NEW;
END $$;
"""

IMMUTABLE = [
    "artifacts",
    "source_files",
    "cpg_nodes",
    "evidence_records",
    "cpg_edges",
    "evidence_derivations",
]
RESULTS = ["cpg_nodes", "evidence_records", "cpg_edges", "evidence_derivations"]


def upgrade() -> None:
    op.execute(SQL)
    for table in IMMUTABLE:
        op.execute(
            f"CREATE TRIGGER guard_immutable BEFORE UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation()"
        )
    for table in ["source_snapshots", "analysis_runs"]:
        op.execute(
            f"CREATE TRIGGER guard_no_delete BEFORE DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation()"
        )
    for table in RESULTS:
        op.execute(
            f"CREATE TRIGGER guard_result_insert BEFORE INSERT ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.codespy_result_guard()"
        )


def downgrade() -> None:
    for table in RESULTS:
        op.execute(f"DROP TRIGGER guard_result_insert ON public.{table}")
    for table in IMMUTABLE:
        op.execute(f"DROP TRIGGER guard_immutable ON public.{table}")
    for table in ["source_snapshots", "analysis_runs"]:
        op.execute(f"DROP TRIGGER guard_no_delete ON public.{table}")
    for table, trigger in [
        ("analysis_runs", "guard_run"),
        ("source_files", "guard_file_insert"),
        ("source_snapshots", "guard_snapshot"),
    ]:
        op.execute(f"DROP TRIGGER {trigger} ON public.{table}")
    for function in [
        "codespy_result_guard",
        "codespy_run_guard",
        "codespy_file_guard",
        "codespy_snapshot_guard",
        "codespy_reject_mutation",
    ]:
        op.execute(f"DROP FUNCTION public.{function}()")
