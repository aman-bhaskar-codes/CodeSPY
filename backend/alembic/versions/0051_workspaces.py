# ruff: noqa: E501
"""Agent workspaces: one per session, an append-only revision log, and checkpoint records.

A revision records what each change did to each path (hashes, never content), the tree hash after
it, and the digest of the request that made it, so a repeated request is recognised and an
interrupted one can be recovered. ``workspaces.head_revision`` is the allocator, as ``last_seq`` is
for agent events: it moves by one per update, a revision's number must equal it, and a deferred
check rejects any transaction that leaves a gap. Revisions and checkpoints are never updated or
deleted; a closed workspace never reopens.
"""

from alembic import op

revision = "0051_workspaces"
down_revision = "0050_agent_sessions"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.workspaces (
 org_id uuid NOT NULL,id uuid NOT NULL,session_id uuid NOT NULL,
 repository_id uuid NOT NULL,base_snapshot_id uuid NOT NULL,
 head_revision bigint NOT NULL DEFAULT 0,sandbox_ref text,
 status text NOT NULL DEFAULT 'open',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 UNIQUE(org_id,session_id),
 FOREIGN KEY(org_id,session_id) REFERENCES public.agent_sessions(org_id,id),
 FOREIGN KEY(org_id,repository_id,base_snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 CONSTRAINT ck_workspace_status CHECK(status IN ('open','closed')),
 CHECK(head_revision>=0),
 CHECK(sandbox_ref IS NULL OR octet_length(sandbox_ref) BETWEEN 1 AND 128)
);

CREATE FUNCTION public.codespy_workspace_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.head_revision<>0 THEN RAISE EXCEPTION 'a new workspace starts at revision 0' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.org_id<>OLD.org_id OR NEW.id<>OLD.id OR NEW.session_id<>OLD.session_id
    OR NEW.repository_id<>OLD.repository_id OR NEW.base_snapshot_id<>OLD.base_snapshot_id
    OR NEW.created_at<>OLD.created_at THEN
  RAISE EXCEPTION 'workspace identity is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.head_revision<OLD.head_revision OR NEW.head_revision>OLD.head_revision+1 THEN
  RAISE EXCEPTION 'workspace revision advances by one' USING ERRCODE='23514'; END IF;
 IF OLD.status='closed' AND NEW.status<>'closed' THEN
  RAISE EXCEPTION 'a closed workspace stays closed' USING ERRCODE='23514'; END IF;
 IF OLD.status='closed' AND NEW.head_revision<>OLD.head_revision THEN
  RAISE EXCEPTION 'a closed workspace takes no revisions' USING ERRCODE='23514'; END IF;
 NEW.updated_at:=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER guard_workspace BEFORE INSERT OR UPDATE ON public.workspaces
 FOR EACH ROW EXECUTE FUNCTION public.codespy_workspace_guard();
CREATE TRIGGER guard_workspace_no_delete BEFORE DELETE ON public.workspaces
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE TABLE public.workspace_revisions (
 org_id uuid NOT NULL,workspace_id uuid NOT NULL,revision bigint NOT NULL,
 author text NOT NULL,change_id text NOT NULL,request_digest text,
 changes_json jsonb NOT NULL,tree_hash text NOT NULL,sandbox_revision bigint,
 recovered boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,workspace_id,revision),
 CONSTRAINT fk_workspace_revision_workspace FOREIGN KEY(org_id,workspace_id) REFERENCES public.workspaces(org_id,id),
 CHECK(revision>=1),
 CHECK(author IN ('agent','user','system')),
 CHECK(change_id ~ '^[0-9a-f]{64}$'),
 CHECK(request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$'),
 CHECK(tree_hash ~ '^[0-9a-f]{64}$'),
 CHECK(jsonb_typeof(changes_json)='array' AND octet_length(changes_json::text)<=262144)
);
CREATE INDEX ix_workspace_revisions_request ON public.workspace_revisions(org_id,workspace_id,request_digest,revision DESC) WHERE request_digest IS NOT NULL;

CREATE FUNCTION public.codespy_workspace_revision_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE head bigint;
BEGIN
 SELECT head_revision INTO head FROM public.workspaces WHERE org_id=NEW.org_id AND id=NEW.workspace_id;
 IF head IS DISTINCT FROM NEW.revision THEN
  RAISE EXCEPTION 'workspace revision must equal the workspace head' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_workspace_revision BEFORE INSERT ON public.workspace_revisions
 FOR EACH ROW EXECUTE FUNCTION public.codespy_workspace_revision_guard();
CREATE TRIGGER guard_workspace_revision_immutable BEFORE UPDATE OR DELETE ON public.workspace_revisions
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE FUNCTION public.codespy_workspace_head_check() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE head bigint; top bigint;
BEGIN
 SELECT head_revision INTO head FROM public.workspaces WHERE org_id=NEW.org_id AND id=NEW.id;
 SELECT coalesce(max(revision),0) INTO top FROM public.workspace_revisions WHERE org_id=NEW.org_id AND workspace_id=NEW.id;
 IF head IS DISTINCT FROM top THEN
  RAISE EXCEPTION 'workspace revision has a gap' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER workspace_head_check AFTER UPDATE ON public.workspaces
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_workspace_head_check();

CREATE TABLE public.workspace_checkpoints (
 org_id uuid NOT NULL,id uuid NOT NULL,workspace_id uuid NOT NULL,
 revision bigint NOT NULL,tree_hash text NOT NULL,label text NOT NULL DEFAULT '',
 sandbox_checkpoint_id uuid,bundle_ref text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 CONSTRAINT fk_workspace_checkpoint_workspace FOREIGN KEY(org_id,workspace_id) REFERENCES public.workspaces(org_id,id),
 CHECK(revision>=0),
 CHECK(tree_hash ~ '^[0-9a-f]{64}$'),
 CHECK(octet_length(label)<=256),
 CHECK(bundle_ref IS NULL OR octet_length(bundle_ref) BETWEEN 1 AND 512)
);
CREATE INDEX ix_workspace_checkpoints_workspace ON public.workspace_checkpoints(org_id,workspace_id,revision DESC);
CREATE TRIGGER guard_workspace_checkpoint_immutable BEFORE UPDATE OR DELETE ON public.workspace_checkpoints
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

REVOKE ALL ON FUNCTION public.codespy_workspace_guard(), public.codespy_workspace_revision_guard(),
 public.codespy_workspace_head_check() FROM PUBLIC;

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT SELECT ON public.workspaces,public.workspace_revisions,public.workspace_checkpoints TO p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT SELECT,INSERT ON public.workspaces,public.workspace_revisions,public.workspace_checkpoints TO p2_broker;
  GRANT UPDATE(head_revision,status,sandbox_ref,updated_at) ON public.workspaces TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
  GRANT SELECT ON public.workspaces,public.workspace_revisions,public.workspace_checkpoints TO p2_operator;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
  GRANT SELECT,INSERT ON public.workspaces,public.workspace_revisions,public.workspace_checkpoints TO codespy_app;
  GRANT UPDATE(head_revision,status,sandbox_ref,updated_at) ON public.workspaces TO codespy_app;
 END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.workspaces) THEN
  RAISE EXCEPTION 'workspace history must be preserved' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.workspace_checkpoints;
DROP TABLE public.workspace_revisions;
DROP TABLE public.workspaces;
DROP FUNCTION public.codespy_workspace_head_check();
DROP FUNCTION public.codespy_workspace_revision_guard();
DROP FUNCTION public.codespy_workspace_guard();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
