"""Retain bounded encrypted effects for scoped investigation recovery."""

from alembic import op

revision = "0039_investigation_effects"
down_revision = "0038_embedding_dispatch_recovery"
branch_labels = None
depends_on = None

SQL = r"""
CREATE TABLE public.investigation_effects (
 org_id uuid NOT NULL,investigation_id uuid NOT NULL,id uuid NOT NULL,
 binding_digest text NOT NULL CHECK(binding_digest~'^[0-9a-f]{64}$'),
 kind text NOT NULL CHECK(kind IN ('model','tool','draft')),
 input_digest text NOT NULL CHECK(input_digest~'^[0-9a-f]{64}$'),
 payload_digest text NOT NULL CHECK(payload_digest~'^[0-9a-f]{64}$'),
 payload_bytes integer NOT NULL CHECK(payload_bytes BETWEEN 1 AND 524288),
 ciphertext bytea,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '7 days'),
 PRIMARY KEY(org_id,investigation_id,id),
 FOREIGN KEY(org_id,investigation_id) REFERENCES public.investigations(org_id,id),
 CHECK(ciphertext IS NULL OR octet_length(ciphertext)<=524316)
);
CREATE FUNCTION public.codespy_effect_scope(p jsonb) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT EXISTS(SELECT 1 FROM public.investigations i WHERE i.org_id=(p->>'org')::uuid
 AND i.id=(p->>'inv')::uuid AND i.repository_id=(p->>'repo')::uuid
 AND i.snapshot_id=(p->>'snapshot')::uuid AND i.principal_id=(p->>'actor')::uuid
 AND i.spec_hash=p->>'spec' AND i.execution_revision=(p->>'revision')::integer
 AND public.codespy_model_actor_write(i.org_id,i.principal_id,i.repository_id))
$$;
CREATE FUNCTION public.codespy_effect_binding(p jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
 SELECT encode(sha256(convert_to(public.codespy_scan_json(p-ARRAY[
 'job','attempt','fence','owner','kind','input_digest','size']),'UTF8')),'hex')
$$;
CREATE FUNCTION public.codespy_put_investigation_effect(p jsonb,c bytea,d text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE j public.jobs%ROWTYPE;a public.job_attempts%ROWTYPE;i public.investigations%ROWTYPE;
 old public.investigation_effects%ROWTYPE; total_bytes bigint;
BEGIN
 SELECT * INTO j FROM public.jobs WHERE org_id=(p->>'org')::uuid AND id=(p->>'job')::uuid FOR
 UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'effect_authority_denied' USING ERRCODE='23514'; END IF;
 SELECT * INTO a FROM public.job_attempts WHERE org_id=j.org_id AND job_id=j.id AND
 id=(p->>'attempt')::uuid FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'effect_authority_denied' USING ERRCODE='23514'; END IF;
 SELECT * INTO i FROM public.investigations WHERE org_id=j.org_id AND id=(p->>'inv')::uuid FOR
 UPDATE;
 IF NOT FOUND OR NOT public.codespy_effect_scope(p) OR j.operation<>'investigate_snapshot'
 OR i.current_job_id IS DISTINCT FROM j.id OR j.current_attempt_id IS DISTINCT FROM a.id
 OR j.state<>'running' OR j.fence IS DISTINCT FROM (p->>'fence')::bigint OR a.fence<>j.fence
 OR a.owner IS DISTINCT FROM p->>'owner' OR a.lease_expires_at<=clock_timestamp()
 OR j.deadline<=clock_timestamp() THEN
 RAISE EXCEPTION 'effect_authority_denied' USING ERRCODE='23514'; END IF;
 SELECT * INTO old FROM public.investigation_effects WHERE org_id=j.org_id AND
 investigation_id=i.id AND id=(p->>'id')::uuid;
 IF FOUND THEN
 IF old.binding_digest IS DISTINCT FROM public.codespy_effect_binding(p) OR old.payload_digest
 IS DISTINCT FROM d
 OR old.kind IS DISTINCT FROM p->>'kind' OR old.input_digest IS DISTINCT FROM p->>'input_digest'
 OR old.expires_at<=clock_timestamp() OR old.ciphertext IS NULL THEN
 RAISE EXCEPTION 'effect_replay_conflict' USING ERRCODE='23514'; END IF;
 RETURN;
 END IF;
 IF c IS NULL OR d IS NULL OR p->>'size' IS NULL OR p->>'input_digest' IS NULL OR p->>'kind' IS NULL
 OR octet_length(c)<>(p->>'size')::integer+28 THEN
 RAISE EXCEPTION 'effect_invalid' USING ERRCODE='23514'; END IF;
 IF p->>'kind' IN ('draft','tool') AND (p->>'size')::integer>262144 THEN
 RAISE EXCEPTION 'effect_size_exceeded' USING ERRCODE='23514'; END IF;
 IF p->>'kind'='tool' THEN
 SELECT coalesce(sum(payload_bytes),0) INTO total_bytes FROM public.investigation_effects
 WHERE org_id=j.org_id AND investigation_id=i.id AND kind='tool';
 IF total_bytes+(p->>'size')::integer>262144 THEN
 RAISE EXCEPTION 'tool_retention_exceeded' USING ERRCODE='23514'; END IF;
 END IF;
 INSERT INTO public.investigation_effects(org_id,investigation_id,id,binding_digest,
 kind,input_digest,payload_digest,payload_bytes,ciphertext)
 VALUES(j.org_id,i.id,(p->>'id')::uuid,public.codespy_effect_binding(p),p->>'kind',p->>'input_digest',d,(p->>'size')::integer,c);
END $$;
CREATE FUNCTION public.codespy_get_investigation_effect(p jsonb)
RETURNS TABLE(ciphertext bytea,payload_digest text) LANGUAGE sql SECURITY DEFINER SET
 search_path=pg_catalog,public AS $$
 SELECT e.ciphertext,e.payload_digest FROM public.investigation_effects e
 WHERE e.org_id=(p->>'org')::uuid AND e.investigation_id=(p->>'inv')::uuid AND e.id=(p->>'id')::uuid
 AND e.binding_digest=public.codespy_effect_binding(p) AND e.expires_at>clock_timestamp()
 AND e.ciphertext IS NOT NULL AND public.codespy_effect_scope(p)
$$;
CREATE FUNCTION public.codespy_expire_investigation_effects(p_limit integer) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE n integer;
BEGIN
 IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
 RAISE EXCEPTION 'effect_cleanup_bounds' USING ERRCODE='23514'; END IF;
 WITH due AS (SELECT org_id,investigation_id,id FROM public.investigation_effects
 WHERE expires_at<=clock_timestamp() AND ciphertext IS NOT NULL ORDER BY expires_at,id LIMIT
 p_limit FOR UPDATE SKIP LOCKED),
 purged AS (
  UPDATE public.investigation_effects e SET ciphertext=NULL FROM due
  WHERE e.org_id=due.org_id AND e.investigation_id=due.investigation_id AND e.id=due.id RETURNING 1
 )
 SELECT count(*) INTO n FROM purged; RETURN n;
END $$;
REVOKE ALL ON public.investigation_effects FROM PUBLIC;
REVOKE ALL ON FUNCTION
 public.codespy_effect_scope,
 public.codespy_effect_binding,
 public.codespy_put_investigation_effect,
 public.codespy_get_investigation_effect,
 public.codespy_expire_investigation_effects FROM PUBLIC;
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
 GRANT EXECUTE ON FUNCTION
 public.codespy_put_investigation_effect,public.codespy_get_investigation_effect,
 public.codespy_expire_investigation_effects TO p2_broker;
 END IF;
END $$;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    raise RuntimeError("encrypted execution history must be retained")
