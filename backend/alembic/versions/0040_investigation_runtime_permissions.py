"""Allow scoped snapshot locks and investigation commands without source mutation."""

from alembic import op

revision = "0040_investigation_runtime_permissions"
down_revision = "0039_investigation_effects"
branch_labels = None
depends_on = None

SQL = r"""
CREATE FUNCTION public.codespy_lock_scoped_snapshot(oid uuid,actor uuid,repo uuid,snapshot uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NOT public.codespy_model_actor_write(oid,actor,repo) THEN RETURN false; END IF;
 PERFORM 1 FROM public.source_snapshots WHERE org_id=oid AND repository_id=repo
 AND id=snapshot AND state='sealed' FOR UPDATE;
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION public.codespy_lock_scoped_snapshot FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
 GRANT EXECUTE ON FUNCTION public.codespy_lock_scoped_snapshot TO p2_api;
 GRANT SELECT,INSERT ON public.investigations TO p2_api;
 GRANT UPDATE(status,current_job_id,deadline,current_step,max_steps,fencing_token,
 worker_id,worker_lease_expires_at,failure_reason,updated_at) ON public.investigations TO p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
 GRANT EXECUTE ON FUNCTION public.codespy_lock_scoped_snapshot TO p2_broker;
 GRANT UPDATE(current_job_id,deadline,failure_reason,worker_id,worker_lease_expires_at)
 ON public.investigations TO p2_broker;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("runtime authority repair must be retained")
