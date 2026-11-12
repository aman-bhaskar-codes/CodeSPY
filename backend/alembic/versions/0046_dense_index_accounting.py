# ruff: noqa: E501
"""Job-bound embedding accounting for the fenced dense index job.

The worker role cannot call the embedding reserve routine and the existing claim/settle routines
are not tied to any job. These SECURITY DEFINER wrappers require a live embed attempt, force the
job's principal, derive the idempotency key from (spec, chunk) and record every reservation in an
immutable ledger. Receipts and staged vectors must agree with that ledger and with settled spend.
"""

from alembic import op

revision = "0046_dense_index_accounting"
down_revision = "0045_dense_index_guards"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.embed_spend_ledger (
 org_id uuid NOT NULL,spec_id uuid NOT NULL,chunk_id uuid NOT NULL,reservation_id uuid NOT NULL,
 job_id uuid NOT NULL,attempt_id uuid NOT NULL,fence bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,spec_id,chunk_id),UNIQUE(org_id,reservation_id),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.embed_specs(org_id,id),
 FOREIGN KEY(org_id,job_id,attempt_id,fence) REFERENCES public.embed_attempt_specs(org_id,job_id,id,fence),
 CHECK(fence>0)
);
CREATE TRIGGER guard_embed_ledger_immutable BEFORE UPDATE OR DELETE ON public.embed_spend_ledger
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE FUNCTION public.codespy_embed_ledger_digest(oid uuid,sid uuid) RETURNS text LANGUAGE sql STABLE SECURITY INVOKER AS $$
 SELECT encode(sha256(convert_to(coalesce(string_agg(chunk_id::text||':'||reservation_id::text,',' ORDER BY chunk_id),''),'UTF8')),'hex')
 FROM public.embed_spend_ledger WHERE org_id=oid AND spec_id=sid
$$;

CREATE FUNCTION public.codespy_embed_reserve(p jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; e public.embed_specs%ROWTYPE; cid uuid; key text; payload jsonb; res jsonb; rid uuid; l public.embed_spend_ledger%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=current_setting('codespy.org_id')::uuid AND id=jid;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=j.org_id AND id=j.embed_spec_id;
 cid:=(p->>'chunk_id')::uuid;
 IF j.operation IS DISTINCT FROM 'embed_chunk_set' OR e.id IS NULL
 OR (p->>'org_id')::uuid IS DISTINCT FROM j.org_id
 OR (p->>'repository_id')::uuid IS DISTINCT FROM j.repository_id
 OR (p->>'principal_id') IS NOT NULL AND (p->>'principal_id')::uuid IS DISTINCT FROM j.principal_id
 OR p->>'provider' IS DISTINCT FROM e.provider OR p->>'model_id' IS DISTINCT FROM e.model_id
 OR NOT EXISTS(SELECT 1 FROM public.source_chunks c WHERE c.org_id=j.org_id AND c.id=cid AND c.chunk_set_id=e.chunk_set_id)
 OR NOT EXISTS(SELECT 1 FROM public.embed_attempt_specs a WHERE a.org_id=j.org_id AND a.job_id=jid AND a.id=j.current_attempt_id AND a.fence=j.fence)
 THEN RAISE EXCEPTION 'embed_scope_denied' USING ERRCODE='42501'; END IF;
 key:='embed:'||e.id::text||':'||cid::text;
 payload:=jsonb_build_object('org_id',j.org_id,'repository_id',j.repository_id,'principal_id',j.principal_id,
  'provider',e.provider,'model_id',e.model_id,'estimated_tokens',p->'estimated_tokens',
  'max_charge_micros',p->'max_charge_micros','idempotency_key',key);
 res:=public.codespy_reserve_embedding_v2(payload);
 rid:=(res->>'reservation_id')::uuid;
 SELECT * INTO l FROM public.embed_spend_ledger WHERE org_id=j.org_id AND spec_id=e.id AND chunk_id=cid;
 IF l.reservation_id IS NULL THEN
  INSERT INTO public.embed_spend_ledger(org_id,spec_id,chunk_id,reservation_id,job_id,attempt_id,fence)
  VALUES(j.org_id,e.id,cid,rid,jid,j.current_attempt_id,j.fence);
 ELSIF l.reservation_id<>rid THEN RAISE EXCEPTION 'embed_ledger_conflict' USING ERRCODE='23514'; END IF;
 RETURN res;
END $$;

CREATE FUNCTION public.codespy_embed_claim(rid uuid) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=current_setting('codespy.org_id')::uuid AND id=jid;
 IF j.operation IS DISTINCT FROM 'embed_chunk_set'
 OR NOT EXISTS(SELECT 1 FROM public.embed_spend_ledger l WHERE l.org_id=j.org_id AND l.reservation_id=rid AND l.spec_id=j.embed_spec_id)
 THEN RAISE EXCEPTION 'embed_scope_denied' USING ERRCODE='42501'; END IF;
 RETURN public.codespy_claim_embedding_dispatch(j.org_id,rid);
END $$;

CREATE FUNCTION public.codespy_embed_settle(rid uuid,p jsonb) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,public AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=current_setting('codespy.org_id')::uuid AND id=jid;
 IF j.operation IS DISTINCT FROM 'embed_chunk_set'
 OR NOT EXISTS(SELECT 1 FROM public.embed_spend_ledger l WHERE l.org_id=j.org_id AND l.reservation_id=rid AND l.spec_id=j.embed_spec_id)
 THEN RAISE EXCEPTION 'embed_scope_denied' USING ERRCODE='42501'; END IF;
 RETURN public.codespy_transition_embedding_v2(j.org_id,rid,p);
END $$;

CREATE FUNCTION public.codespy_embed_stage_guard_v2() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; e public.embed_specs%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN
  jid:=public.codespy_embed_authority(OLD.org_id,OLD.snapshot_id);
  IF NOT EXISTS(SELECT 1 FROM public.embed_publication_receipts r WHERE r.org_id=OLD.org_id AND r.spec_id=OLD.spec_id AND r.job_id=jid)
  THEN RAISE EXCEPTION 'staged embedding is retained until publication' USING ERRCODE='23514'; END IF;
  RETURN OLD;
 END IF;
 jid:=public.codespy_embed_authority(NEW.org_id,NEW.snapshot_id);
 SELECT es.* INTO e FROM public.embed_specs es JOIN public.jobs j ON j.org_id=es.org_id AND j.embed_spec_id=es.id
  WHERE j.org_id=NEW.org_id AND j.id=jid;
 IF NEW.spec_id IS DISTINCT FROM e.id OR NEW.chunk_set_id IS DISTINCT FROM e.chunk_set_id
 OR NEW.repository_id IS DISTINCT FROM e.repository_id OR NEW.snapshot_id IS DISTINCT FROM e.snapshot_id
 OR jsonb_array_length(NEW.embedding_json) IS DISTINCT FROM e.dimension
 THEN RAISE EXCEPTION 'embed staged vector differs' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.embed_spend_ledger l JOIN public.embedding_reservations r
   ON r.org_id=l.org_id AND r.reservation_id=l.reservation_id
   WHERE l.org_id=NEW.org_id AND l.spec_id=e.id AND l.chunk_id=NEW.chunk_id AND l.reservation_id=NEW.reservation_id
   AND r.repository_id=NEW.repository_id AND r.provider=e.provider AND r.model_id=e.model_id AND r.state='committed'
   AND r.actual_tokens=NEW.token_count)
 THEN RAISE EXCEPTION 'embed staged vector has no settled reservation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_receipt_guard_v2() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; e public.embed_specs%ROWTYPE; settled bigint; tokens bigint;
BEGIN
 jid:=public.codespy_embed_authority(NEW.org_id,NEW.snapshot_id);
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=NEW.org_id AND id=j.embed_spec_id;
 SELECT count(*),coalesce(sum(r.actual_tokens),0) INTO settled,tokens FROM public.embed_spend_ledger l
  JOIN public.embedding_reservations r ON r.org_id=l.org_id AND r.reservation_id=l.reservation_id
  WHERE l.org_id=NEW.org_id AND l.spec_id=e.id AND r.state='committed';
 IF NEW.job_id IS DISTINCT FROM jid OR NEW.attempt_id IS DISTINCT FROM j.current_attempt_id OR NEW.fence IS DISTINCT FROM j.fence
 OR NEW.spec_id IS DISTINCT FROM e.id OR NEW.spec_hash IS DISTINCT FROM e.spec_hash
 OR NEW.chunk_set_id IS DISTINCT FROM e.chunk_set_id OR NEW.repository_id IS DISTINCT FROM j.repository_id
 OR NEW.snapshot_id IS DISTINCT FROM j.snapshot_id
 OR NEW.index_id IS DISTINCT FROM public.uuid_generate_v5(e.chunk_set_id,'dense-v1:'||e.provider||':'||e.model_id||':'||e.dimension::text)
 OR NEW.embedding_digest IS DISTINCT FROM public.codespy_embed_embedding_digest(NEW.org_id,NEW.index_id)
 OR NEW.ledger_digest IS DISTINCT FROM public.codespy_embed_ledger_digest(NEW.org_id,e.id)
 OR NEW.chunk_count IS DISTINCT FROM (SELECT count(*) FROM public.chunk_embeddings WHERE org_id=NEW.org_id AND index_id=NEW.index_id)
 OR settled IS DISTINCT FROM NEW.chunk_count OR tokens IS DISTINCT FROM NEW.token_total
 OR EXISTS(SELECT 1 FROM public.chunk_embeddings c WHERE c.org_id=NEW.org_id AND c.index_id=NEW.index_id
    AND NOT EXISTS(SELECT 1 FROM public.embed_spend_ledger l WHERE l.org_id=c.org_id AND l.spec_id=e.id AND l.chunk_id=c.chunk_id))
 THEN RAISE EXCEPTION 'embed receipt differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

DROP TRIGGER guard_embed_stage ON public.embed_vector_stage;
CREATE TRIGGER guard_embed_stage BEFORE INSERT OR DELETE ON public.embed_vector_stage FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_stage_guard_v2();
DROP TRIGGER guard_embed_receipt ON public.embed_publication_receipts;
CREATE TRIGGER guard_embed_receipt BEFORE INSERT ON public.embed_publication_receipts FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_receipt_guard_v2();

REVOKE ALL ON FUNCTION public.codespy_embed_ledger_digest(uuid,uuid),public.codespy_embed_reserve(jsonb),
 public.codespy_embed_claim(uuid),public.codespy_embed_settle(uuid,jsonb),
 public.codespy_embed_stage_guard_v2(),public.codespy_embed_receipt_guard_v2() FROM PUBLIC;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.embed_spend_ledger) THEN
  RAISE EXCEPTION 'dense index spend history exists' USING ERRCODE='23514'; END IF;
END $$;
DROP TRIGGER guard_embed_receipt ON public.embed_publication_receipts;
CREATE TRIGGER guard_embed_receipt BEFORE INSERT ON public.embed_publication_receipts FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_receipt_guard();
DROP TRIGGER guard_embed_stage ON public.embed_vector_stage;
CREATE TRIGGER guard_embed_stage BEFORE INSERT OR DELETE ON public.embed_vector_stage FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_stage_guard();
DROP FUNCTION public.codespy_embed_receipt_guard_v2();
DROP FUNCTION public.codespy_embed_stage_guard_v2();
DROP FUNCTION public.codespy_embed_settle(uuid,jsonb);
DROP FUNCTION public.codespy_embed_claim(uuid);
DROP FUNCTION public.codespy_embed_reserve(jsonb);
DROP FUNCTION public.codespy_embed_ledger_digest(uuid,uuid);
DROP TABLE public.embed_spend_ledger;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
