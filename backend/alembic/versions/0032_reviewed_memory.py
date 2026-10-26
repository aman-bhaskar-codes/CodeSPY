"""Reviewed repository knowledge with versioned history and bounded retention."""

from alembic import op

revision = "0032_reviewed_memory"
down_revision = "0031_investigation_execution"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.repo_memories
 ADD COLUMN state varchar(24) NOT NULL DEFAULT 'needs_review',
 ADD COLUMN version integer NOT NULL DEFAULT 1,
 ADD COLUMN creator_id uuid,
 ADD COLUMN reviewer_id uuid,
 ADD COLUMN accepted_at timestamptz,
 ADD COLUMN expires_at timestamptz,
 ADD COLUMN purge_after timestamptz,
 ADD COLUMN purged_at timestamptz,
 ADD COLUMN content_digest text,
 ADD CONSTRAINT uq_memory_scope UNIQUE(org_id, repository_id, id),
 ADD CONSTRAINT fk_mem_scoped_snapshot FOREIGN KEY(org_id, repository_id, snapshot_id)
   REFERENCES public.source_snapshots(org_id, repository_id, id),
 ADD CONSTRAINT ck_memory_version CHECK(version >= 1),
 ADD CONSTRAINT ck_memory_state CHECK(state IN
   ('needs_review','candidate','accepted','rejected','archived','superseded','expired'));
UPDATE public.repo_memories SET expires_at=clock_timestamp()+interval '7 days',
 purge_after=clock_timestamp()+interval '14 days',
 tsv=to_tsvector('simple', topic || ' ' || summary);
CREATE INDEX ix_memory_eligible ON public.repo_memories(org_id, repository_id, state,
 expires_at, id);
CREATE INDEX ix_memory_cleanup ON public.repo_memories(purge_after, id) WHERE purged_at IS NULL;
CREATE TABLE public.memory_versions (
 org_id uuid NOT NULL, repository_id uuid NOT NULL, memory_id uuid NOT NULL,
 version integer NOT NULL CHECK(version>=1), content_digest text NOT NULL,
 payload jsonb, purged_at timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,memory_id,version),
 FOREIGN KEY(org_id,repository_id,memory_id) REFERENCES
 public.repo_memories(org_id,repository_id,id)
);
CREATE TABLE public.memory_review_events (
 org_id uuid NOT NULL, repository_id uuid NOT NULL, id uuid NOT NULL,
 memory_id uuid NOT NULL, actor_id uuid NOT NULL, version integer NOT NULL,
 action text NOT NULL, reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 1024),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), PRIMARY KEY(org_id,id),
 FOREIGN KEY(org_id,repository_id,memory_id) REFERENCES
 public.repo_memories(org_id,repository_id,id)
);
CREATE TABLE public.memory_commands (
 org_id uuid NOT NULL, repository_id uuid NOT NULL, request_key text NOT NULL,
 actor_id uuid NOT NULL, request_digest text NOT NULL, memory_id uuid NOT NULL,
 version integer NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,repository_id,request_key),
 FOREIGN KEY(org_id,repository_id,memory_id) REFERENCES
 public.repo_memories(org_id,repository_id,id)
);
CREATE TABLE public.context_manifests (
 org_id uuid NOT NULL, repository_id uuid NOT NULL, snapshot_id uuid NOT NULL,
 investigation_id uuid NOT NULL, logical_call_id uuid NOT NULL, principal_id uuid NOT NULL,
 selection_digest text NOT NULL, manifest jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,investigation_id,logical_call_id),
 FOREIGN KEY(org_id,repository_id,snapshot_id)
   REFERENCES public.source_snapshots(org_id,repository_id,id),
 FOREIGN KEY(org_id,investigation_id) REFERENCES public.investigations(org_id,id)
);
CREATE TRIGGER guard_context_manifests BEFORE UPDATE OR DELETE ON public.context_manifests
 FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();
DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['p2_api','p2_broker','p2_operator','codespy_app'] LOOP
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
 EXECUTE format('GRANT SELECT ON public.context_manifests TO %I',r);
 END IF;
 END LOOP;
 GRANT INSERT ON public.context_manifests TO p2_broker,codespy_app;
END $$;
UPDATE public.repo_memories SET content_digest=encode(sha256(convert_to(
 topic || chr(10) || summary || chr(10) || evidence_pointers::text,'UTF8')),'hex');
INSERT INTO public.memory_versions(org_id,repository_id,memory_id,version,content_digest,payload)
SELECT org_id,repository_id,id,version,content_digest,jsonb_build_object(
 'memory_id',id,'org_id',org_id,'repository_id',repository_id,'snapshot_id',snapshot_id,
 'memory_kind',memory_kind,'topic',topic,'summary',summary,'evidence_pointers',evidence_pointers,
 'created_at',created_at,'updated_at',updated_at,'state','needs_review','version',version,
 'content_digest',content_digest,'expires_at',expires_at)
FROM public.repo_memories;
CREATE FUNCTION public.codespy_memory_projection_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'memory identity is retained' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' THEN
   IF NEW.state <> 'candidate' OR NEW.version <> 1 OR NEW.reviewer_id IS NOT NULL
      OR NEW.accepted_at IS NOT NULL OR NEW.expires_at IS NULL
      OR NEW.expires_at > clock_timestamp()+interval '7 days'
   THEN RAISE EXCEPTION 'only bounded candidates may be inserted' USING ERRCODE='23514'; END IF;
 ELSE
   IF ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.snapshot_id,NEW.creator_id,NEW.created_at)
      IS DISTINCT FROM
 ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.snapshot_id,OLD.creator_id,OLD.created_at)
   THEN RAISE EXCEPTION 'memory identity is immutable' USING ERRCODE='23514'; END IF;
   IF NEW.purged_at IS NOT NULL AND OLD.purged_at IS NULL THEN
     IF current_user NOT IN ('p2_operator','p2_migration') OR OLD.purge_after > clock_timestamp()
        OR OLD.purge_after IS NULL OR NEW.topic <> '[purged]' OR NEW.summary <> ''
        OR NEW.evidence_pointers <> '[]'::jsonb OR NEW.version <> OLD.version
     THEN RAISE EXCEPTION 'memory purge unavailable' USING ERRCODE='23514'; END IF;
   ELSE
     IF current_user NOT IN ('p2_api','p2_migration','codespy_app')
        OR OLD.purged_at IS NOT NULL OR NEW.version <> OLD.version+1
        OR ROW(NEW.topic,NEW.summary,NEW.memory_kind,NEW.evidence_pointers,NEW.content_digest)
          IS DISTINCT FROM
 ROW(OLD.topic,OLD.summary,OLD.memory_kind,OLD.evidence_pointers,OLD.content_digest)
        OR NOT EXISTS(SELECT 1 FROM public.memberships m JOIN public.repository_grants g
          ON g.org_id=m.org_id AND g.principal_id=m.principal_id
          WHERE m.org_id=NEW.org_id AND m.principal_id=NEW.reviewer_id
            AND m.role IN ('owner','admin') AND g.repository_id=NEW.repository_id
            AND g.permission='write' AND g.revoked_at IS NULL)
     THEN RAISE EXCEPTION 'memory review authority unavailable' USING ERRCODE='23514'; END IF;
     IF NOT (
       (OLD.state IN ('candidate','needs_review') AND NEW.state IN ('accepted','rejected') AND
 OLD.expires_at>clock_timestamp()) OR
       (OLD.state='accepted' AND NEW.state IN ('archived','superseded') AND
 OLD.expires_at>clock_timestamp()) OR
       ((OLD.state='archived' OR (OLD.state IN ('accepted','candidate','needs_review') AND
 OLD.expires_at<=clock_timestamp())) AND NEW.state='accepted')
     ) THEN RAISE EXCEPTION 'invalid memory transition' USING ERRCODE='23514'; END IF;
     IF NEW.state='accepted' AND (NEW.accepted_at IS NULL OR NEW.expires_at IS NULL
        OR NEW.expires_at <> NEW.accepted_at+interval '90 days'
        OR NEW.accepted_at > clock_timestamp())
     THEN RAISE EXCEPTION 'invalid memory lifetime' USING ERRCODE='23514'; END IF;
   END IF;
 END IF;
 IF octet_length(NEW.summary)>16384 OR length(NEW.summary)>4096
    OR jsonb_array_length(NEW.evidence_pointers)>10
 THEN RAISE EXCEPTION 'memory payload limit' USING ERRCODE='23514'; END IF;
 NEW.tsv := to_tsvector('simple',NEW.topic || ' ' || NEW.summary);
 RETURN NEW;
END $$;
CREATE TRIGGER guard_memory_projection BEFORE INSERT OR UPDATE OR DELETE ON public.repo_memories
 FOR EACH ROW EXECUTE FUNCTION public.codespy_memory_projection_guard();
CREATE FUNCTION public.codespy_memory_version_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,public AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'memory history is retained' USING ERRCODE='23514'; END IF;
 IF current_user NOT IN ('p2_operator','p2_migration') OR NEW.payload IS NOT NULL
   OR NEW.purged_at IS NULL OR OLD.payload IS NULL
   OR ROW(NEW.org_id,NEW.repository_id,NEW.memory_id,NEW.version,NEW.content_digest,NEW.created_at)
      IS DISTINCT FROM
 ROW(OLD.org_id,OLD.repository_id,OLD.memory_id,OLD.version,OLD.content_digest,OLD.created_at)
   OR NOT EXISTS(SELECT 1 FROM public.repo_memories m WHERE m.org_id=OLD.org_id
       AND m.id=OLD.memory_id AND m.purge_after<=clock_timestamp())
 THEN RAISE EXCEPTION 'memory version is immutable except expired payload purge' USING
 ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_memory_versions BEFORE UPDATE OR DELETE ON public.memory_versions
 FOR EACH ROW EXECUTE FUNCTION public.codespy_memory_version_guard();
CREATE TRIGGER guard_memory_review_events BEFORE UPDATE OR DELETE ON public.memory_review_events
 FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();
CREATE TRIGGER guard_memory_commands BEFORE UPDATE OR DELETE ON public.memory_commands
 FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();
CREATE FUNCTION public.codespy_purge_memories(batch integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE item record; changed integer := 0; stamp timestamptz := clock_timestamp();
BEGIN
 IF batch < 1 OR batch > 100 OR batch IS NULL THEN
   RAISE EXCEPTION 'invalid cleanup batch' USING ERRCODE='23514';
 END IF;
 FOR item IN SELECT org_id,id FROM public.repo_memories
   WHERE purged_at IS NULL AND purge_after<=stamp
   ORDER BY purge_after,id LIMIT batch FOR UPDATE SKIP LOCKED
 LOOP
   UPDATE public.memory_versions SET payload=NULL,purged_at=stamp
     WHERE org_id=item.org_id AND memory_id=item.id AND payload IS NOT NULL;
   UPDATE public.repo_memories SET topic='[purged]',summary='',evidence_pointers='[]'::jsonb,
     purged_at=stamp WHERE org_id=item.org_id AND id=item.id;
   changed := changed+1;
 END LOOP;
 RETURN changed;
END $$;
REVOKE ALL ON FUNCTION public.codespy_purge_memories(integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.codespy_purge_memories(integer) TO p2_broker;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
 GRANT EXECUTE ON FUNCTION public.codespy_purge_memories(integer) TO p2_operator;
 END IF;
END $$;

DO $$ DECLARE r text; BEGIN
 FOREACH r IN ARRAY ARRAY['p2_api','p2_broker','p2_operator','codespy_app'] LOOP
   IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname=r) THEN
     EXECUTE format('GRANT SELECT ON
 public.repo_memories,public.memory_versions,public.memory_review_events,public.memory_commands
 TO %I',r);
   END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
   GRANT INSERT,UPDATE ON public.repo_memories TO p2_api;
   GRANT INSERT ON public.memory_versions,public.memory_review_events,public.memory_commands TO
 p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
   GRANT INSERT ON public.repo_memories,public.memory_versions,public.memory_commands TO p2_broker;
   REVOKE UPDATE,DELETE ON public.repo_memories FROM p2_broker;
   GRANT UPDATE(updated_at) ON public.repo_memories TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
   GRANT UPDATE ON public.repo_memories,public.memory_versions TO p2_operator;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
   GRANT INSERT,UPDATE ON
 public.memory_versions,public.memory_review_events,public.memory_commands TO codespy_app;
   REVOKE DELETE ON public.repo_memories FROM codespy_app;
 END IF;
END $$;
"""

DOWN = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.repo_memories) OR EXISTS(SELECT 1 FROM public.memory_commands)
    OR EXISTS(SELECT 1 FROM public.context_manifests)
 THEN RAISE EXCEPTION 'memory review history must be preserved'; END IF;
END $$;
DROP TABLE public.context_manifests, public.memory_commands, public.memory_review_events,
 public.memory_versions;
DROP TRIGGER guard_memory_projection ON public.repo_memories;
DROP FUNCTION public.codespy_memory_projection_guard(), public.codespy_memory_version_guard(),
 public.codespy_purge_memories(integer);
DROP INDEX public.ix_memory_eligible, public.ix_memory_cleanup;
ALTER TABLE public.repo_memories DROP CONSTRAINT fk_mem_scoped_snapshot,
 DROP CONSTRAINT uq_memory_scope, DROP CONSTRAINT ck_memory_version, DROP CONSTRAINT
 ck_memory_state,
 DROP COLUMN state, DROP COLUMN version, DROP COLUMN creator_id, DROP COLUMN reviewer_id,
 DROP COLUMN accepted_at, DROP COLUMN expires_at, DROP COLUMN purge_after,
 DROP COLUMN purged_at, DROP COLUMN content_digest;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN)
