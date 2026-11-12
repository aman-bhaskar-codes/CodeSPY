"""Lock a sealed snapshot for recovery without checking the actor's current access.

Recovery must close a job even when its principal has lost write access. Since revision 0040 the
only snapshot-locking function runs the actor-write check unconditionally, so recovery of a revoked
writer raised and could abort a whole recovery pass. This function takes the same lock on the same
rows (a sealed snapshot of the named repository) and performs no actor check. It is granted to the
worker role only; every normal authorization path keeps using `codespy_lock_scoped_snapshot`.
"""

from alembic import op

revision = "0047_recovery_snapshot_lock"
down_revision = "0046_dense_index_accounting"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';
CREATE FUNCTION public.codespy_lock_snapshot_for_recovery(oid uuid,repo uuid,snapshot uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 PERFORM 1 FROM public.source_snapshots WHERE org_id=oid AND repository_id=repo
 AND id=snapshot AND state='sealed' FOR UPDATE;
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION public.codespy_lock_snapshot_for_recovery(uuid,uuid,uuid) FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT EXECUTE ON FUNCTION public.codespy_lock_snapshot_for_recovery(uuid,uuid,uuid) TO p2_broker;
 END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DROP FUNCTION public.codespy_lock_snapshot_for_recovery(uuid,uuid,uuid);
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
