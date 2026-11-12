# ruff: noqa: E501
"""Worker-fenced authority and commit guards for dense index publication.

A dense index may be written only by a live, fenced `embed_chunk_set` attempt bound to an
immutable embed spec, over a chunk set that already has a lexical publication receipt. Published
vectors must equal the vectors staged against settled reservations. Updates and deletes of
published rows stay rejected, and lexical publication guards are unchanged.
"""

from alembic import op

revision = "0045_dense_index_guards"
down_revision = "0044_dense_index_schema"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

CREATE FUNCTION public.codespy_embed_authority(oid uuid,sid uuid) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; e public.embed_specs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=oid AND id=jid;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=oid AND id=j.embed_spec_id;
 IF j.operation IS DISTINCT FROM 'embed_chunk_set' OR j.snapshot_id IS DISTINCT FROM sid OR e.id IS NULL
 OR e.principal_id IS DISTINCT FROM j.principal_id OR e.config_hash IS DISTINCT FROM j.config_hash
 OR NOT EXISTS(SELECT 1 FROM public.embed_attempt_specs a WHERE a.org_id=oid AND a.job_id=jid
  AND a.id=j.current_attempt_id AND a.fence=j.fence AND a.spec_id=e.id AND a.spec_hash=e.spec_hash)
 OR NOT EXISTS(SELECT 1 FROM public.index_publication_receipts r WHERE r.org_id=oid
  AND r.chunk_set_id=e.chunk_set_id AND r.index_id=e.lexical_index_id)
 THEN RAISE EXCEPTION 'embed worker binding differs' USING ERRCODE='23514'; END IF;
 RETURN jid;
END $$;

CREATE FUNCTION public.codespy_embed_embedding_digest(oid uuid,iid uuid) RETURNS text LANGUAGE sql STABLE SECURITY INVOKER AS $$
 SELECT encode(sha256(convert_to(coalesce(string_agg(chunk_id::text||':'||encode(sha256(convert_to(embedding_json::text,'UTF8')),'hex'),',' ORDER BY chunk_id),''),'UTF8')),'hex')
 FROM public.chunk_embeddings WHERE org_id=oid AND index_id=iid
$$;

CREATE FUNCTION public.codespy_embed_spec_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.retrieval_chunk_sets s
   JOIN public.index_publication_receipts r ON r.org_id=s.org_id AND r.chunk_set_id=s.id
   JOIN public.search_indices i ON i.org_id=s.org_id AND i.id=r.index_id AND i.chunk_set_id=s.id
   WHERE s.org_id=NEW.org_id AND s.id=NEW.chunk_set_id AND s.repository_id=NEW.repository_id
   AND s.snapshot_id=NEW.snapshot_id AND r.index_id=NEW.lexical_index_id
   AND i.index_type='lexical' AND i.state IN ('ready','partial_ready') AND s.chunk_count>0)
 OR NOT EXISTS(SELECT 1 FROM public.source_snapshots ss WHERE ss.org_id=NEW.org_id AND ss.id=NEW.snapshot_id
   AND ss.repository_id=NEW.repository_id AND ss.state='sealed')
 THEN RAISE EXCEPTION 'embed chunk set unavailable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_attempt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; e public.embed_specs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=NEW.org_id AND id=j.embed_spec_id;
 IF NEW.job_id IS DISTINCT FROM jid OR NEW.id IS DISTINCT FROM j.current_attempt_id OR NEW.fence IS DISTINCT FROM j.fence
 OR j.operation IS DISTINCT FROM 'embed_chunk_set' OR e.id IS NULL OR NEW.spec_id IS DISTINCT FROM e.id
 OR NEW.spec_hash IS DISTINCT FROM e.spec_hash OR NEW.repository_id IS DISTINCT FROM j.repository_id
 OR NEW.snapshot_id IS DISTINCT FROM j.snapshot_id
 THEN RAISE EXCEPTION 'embed attempt differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_stage_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
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
 IF NOT EXISTS(SELECT 1 FROM public.embedding_reservations r WHERE r.org_id=NEW.org_id AND r.reservation_id=NEW.reservation_id
   AND r.repository_id=NEW.repository_id AND r.provider=e.provider AND r.model_id=e.model_id AND r.state='committed')
 THEN RAISE EXCEPTION 'embed staged vector has no settled reservation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_receipt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; e public.embed_specs%ROWTYPE;
BEGIN
 jid:=public.codespy_embed_authority(NEW.org_id,NEW.snapshot_id);
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=NEW.org_id AND id=j.embed_spec_id;
 IF NEW.job_id IS DISTINCT FROM jid OR NEW.attempt_id IS DISTINCT FROM j.current_attempt_id OR NEW.fence IS DISTINCT FROM j.fence
 OR NEW.spec_id IS DISTINCT FROM e.id OR NEW.spec_hash IS DISTINCT FROM e.spec_hash
 OR NEW.chunk_set_id IS DISTINCT FROM e.chunk_set_id OR NEW.repository_id IS DISTINCT FROM j.repository_id
 OR NEW.snapshot_id IS DISTINCT FROM j.snapshot_id
 OR NEW.index_id IS DISTINCT FROM public.uuid_generate_v5(e.chunk_set_id,'dense-v1:'||e.provider||':'||e.model_id||':'||e.dimension::text)
 OR NEW.embedding_digest IS DISTINCT FROM public.codespy_embed_embedding_digest(NEW.org_id,NEW.index_id)
 OR NEW.chunk_count IS DISTINCT FROM (SELECT count(*) FROM public.chunk_embeddings WHERE org_id=NEW.org_id AND index_id=NEW.index_id)
 THEN RAISE EXCEPTION 'embed receipt differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_job_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE r public.embed_publication_receipts%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.operation='embed_chunk_set' OR NEW.operation='embed_chunk_set') AND
 ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.snapshot_id,OLD.principal_id,OLD.embed_spec_id,OLD.config_hash,OLD.operation,OLD.request_key,OLD.request_hash,OLD.retry_of_job_id)
 IS DISTINCT FROM ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.snapshot_id,NEW.principal_id,NEW.embed_spec_id,NEW.config_hash,NEW.operation,NEW.request_key,NEW.request_hash,NEW.retry_of_job_id)
 THEN RAISE EXCEPTION 'embed job identity immutable' USING ERRCODE='23514'; END IF;
 IF NEW.operation='embed_chunk_set' AND NEW.state IN ('succeeded','partial') THEN
  SELECT * INTO r FROM public.embed_publication_receipts WHERE org_id=NEW.org_id AND job_id=NEW.id AND attempt_id=NEW.current_attempt_id AND fence=NEW.fence;
  IF r.job_id IS NULL OR NEW.result_index_id IS DISTINCT FROM r.index_id
   OR (NEW.state='succeeded') IS DISTINCT FROM (r.coverage='complete')
  THEN RAISE EXCEPTION 'embed terminal receipt differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_embed_verify(oid uuid,iid uuid) RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE p public.embed_publication_receipts%ROWTYPE; j public.jobs%ROWTYPE; a public.job_attempts%ROWTYPE;
 s public.retrieval_chunk_sets%ROWTYPE; i public.search_indices%ROWTYPE; e public.embed_specs%ROWTYPE; n bigint; want text;
BEGIN
 SELECT * INTO p FROM public.embed_publication_receipts WHERE org_id=oid AND index_id=iid;
 SELECT * INTO i FROM public.search_indices WHERE org_id=oid AND id=iid;
 SELECT * INTO j FROM public.jobs WHERE org_id=oid AND id=p.job_id;
 SELECT * INTO a FROM public.job_attempts WHERE org_id=oid AND job_id=p.job_id AND id=p.attempt_id;
 SELECT * INTO s FROM public.retrieval_chunk_sets WHERE org_id=oid AND id=p.chunk_set_id;
 SELECT * INTO e FROM public.embed_specs WHERE org_id=oid AND id=p.spec_id;
 SELECT count(*) INTO n FROM public.chunk_embeddings WHERE org_id=oid AND index_id=iid;
 want:=CASE WHEN s.coverage='complete' THEN 'ready' ELSE 'partial_ready' END;
 IF p.job_id IS NULL OR i.id IS NULL OR j.id IS NULL OR a.id IS NULL OR s.id IS NULL OR e.id IS NULL
 OR j.operation<>'embed_chunk_set' OR j.state NOT IN ('succeeded','partial') OR j.result_index_id IS DISTINCT FROM iid
 OR j.current_attempt_id IS DISTINCT FROM p.attempt_id OR j.fence IS DISTINCT FROM p.fence
 OR a.state<>'succeeded' OR j.publication_checked_at IS NULL
 OR (j.state='succeeded')<>(s.coverage='complete') OR p.coverage<>s.coverage
 OR i.index_type<>'dense' OR i.chunk_set_id IS DISTINCT FROM s.id
 OR i.embedding_provider IS DISTINCT FROM e.provider OR i.embedding_model IS DISTINCT FROM e.model_id OR i.dimension IS DISTINCT FROM e.dimension
 OR e.chunk_set_id<>s.id OR i.state<>want
 OR n<>s.chunk_count OR p.chunk_count<>s.chunk_count OR i.total_chunks<>s.chunk_count
 OR i.indexed_chunks<>s.chunk_count OR i.failed_chunks<>0
 OR p.embedding_digest<>public.codespy_embed_embedding_digest(oid,iid)
 THEN RAISE EXCEPTION 'uncommitted dense publication' USING ERRCODE='23514'; END IF;
END $$;

CREATE FUNCTION public.codespy_embed_committed_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE iid uuid;
BEGIN
 IF TG_TABLE_NAME='chunk_embeddings' THEN
  IF NOT EXISTS(SELECT 1 FROM public.embed_publication_receipts WHERE org_id=NEW.org_id AND index_id=NEW.index_id)
  THEN RAISE EXCEPTION 'uncommitted dense embeddings' USING ERRCODE='23514'; END IF;
  RETURN NULL;
 END IF;
 IF TG_TABLE_NAME='jobs' THEN
  SELECT index_id INTO iid FROM public.embed_publication_receipts WHERE org_id=NEW.org_id AND job_id=NEW.id;
 ELSIF TG_TABLE_NAME='search_indices' THEN iid:=NEW.id; ELSE iid:=NEW.index_id; END IF;
 IF iid IS NULL THEN RAISE EXCEPTION 'uncommitted dense publication' USING ERRCODE='23514'; END IF;
 PERFORM public.codespy_embed_verify(NEW.org_id,iid);
 RETURN NULL;
END $$;

CREATE OR REPLACE FUNCTION public.codespy_index_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; setid uuid; oid uuid; sid uuid; rid uuid; cfg text; s public.retrieval_chunk_sets%ROWTYPE;
 ix public.search_indices%ROWTYPE; e public.embed_specs%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='chunk_embeddings' THEN
  IF TG_OP='INSERT' AND NEW.chunk_set_id IS NOT NULL THEN
   -- Dense publication: only the fenced embed attempt, only vectors staged against settled spend.
   SELECT * INTO ix FROM public.search_indices WHERE org_id=NEW.org_id AND id=NEW.index_id;
   jid:=public.codespy_embed_authority(NEW.org_id,NEW.snapshot_id);
   SELECT es.* INTO e FROM public.embed_specs es JOIN public.jobs j ON j.org_id=es.org_id AND j.embed_spec_id=es.id
    WHERE j.org_id=NEW.org_id AND j.id=jid;
   IF ix.id IS NULL OR ix.index_type<>'dense' OR ix.chunk_set_id IS DISTINCT FROM NEW.chunk_set_id
   OR e.chunk_set_id IS DISTINCT FROM NEW.chunk_set_id OR ix.embedding_provider IS DISTINCT FROM e.provider
   OR ix.embedding_model IS DISTINCT FROM e.model_id OR ix.dimension IS DISTINCT FROM e.dimension
   OR ix.id IS DISTINCT FROM public.uuid_generate_v5(e.chunk_set_id,'dense-v1:'||e.provider||':'||e.model_id||':'||e.dimension::text)
   OR jsonb_array_length(NEW.embedding_json) IS DISTINCT FROM e.dimension
   OR NOT EXISTS(SELECT 1 FROM public.embed_vector_stage g WHERE g.org_id=NEW.org_id AND g.spec_id=e.id
     AND g.chunk_id=NEW.chunk_id AND g.embedding_json=NEW.embedding_json AND g.token_count=NEW.token_count)
   THEN RAISE EXCEPTION 'qualified embedding publication unavailable' USING ERRCODE='23514'; END IF;
   IF EXISTS(SELECT 1 FROM public.embed_publication_receipts WHERE org_id=NEW.org_id AND index_id=NEW.index_id)
   THEN RAISE EXCEPTION 'sealed dense index cannot be appended' USING ERRCODE='23514'; END IF;
   RETURN NEW;
  END IF;
  IF TG_OP<>'DELETE' AND (NEW.chunk_set_id IS NOT NULL OR
    EXISTS(SELECT 1 FROM public.source_chunks WHERE org_id=NEW.org_id AND id=NEW.chunk_id AND chunk_set_id IS NOT NULL) OR
    EXISTS(SELECT 1 FROM public.search_indices WHERE org_id=NEW.org_id AND id=NEW.index_id AND chunk_set_id IS NOT NULL))
  THEN RAISE EXCEPTION 'qualified embedding publication unavailable' USING ERRCODE='23514'; END IF;
  IF TG_OP<>'INSERT' AND (OLD.chunk_set_id IS NOT NULL OR
    EXISTS(SELECT 1 FROM public.source_chunks WHERE org_id=OLD.org_id AND id=OLD.chunk_id AND chunk_set_id IS NOT NULL) OR
    EXISTS(SELECT 1 FROM public.search_indices WHERE org_id=OLD.org_id AND id=OLD.index_id AND chunk_set_id IS NOT NULL))
  THEN RAISE EXCEPTION 'qualified embedding mutation unavailable' USING ERRCODE='23514'; END IF;
  RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
 END IF;
 IF TG_TABLE_NAME='search_indices' AND TG_OP='INSERT' THEN
 IF NEW.chunk_set_id IS NOT NULL AND NEW.index_type='dense' THEN
  jid:=public.codespy_embed_authority(NEW.org_id,NEW.snapshot_id);
  SELECT es.* INTO e FROM public.embed_specs es JOIN public.jobs j ON j.org_id=es.org_id AND j.embed_spec_id=es.id
   WHERE j.org_id=NEW.org_id AND j.id=jid;
  IF e.chunk_set_id IS DISTINCT FROM NEW.chunk_set_id OR e.repository_id IS DISTINCT FROM NEW.repository_id
  OR NEW.embedding_provider IS DISTINCT FROM e.provider OR NEW.embedding_model IS DISTINCT FROM e.model_id
  OR NEW.dimension IS DISTINCT FROM e.dimension
  THEN RAISE EXCEPTION 'dense index scope differs' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM public.embed_publication_receipts WHERE org_id=NEW.org_id AND index_id=NEW.id)
  THEN RAISE EXCEPTION 'sealed dense index cannot be appended' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 END IF;
 IF TG_TABLE_NAME='source_chunks' OR TG_TABLE_NAME='search_indices' THEN
  IF TG_OP<>'INSERT' THEN
   IF OLD.chunk_set_id IS NOT NULL OR NEW.chunk_set_id IS NOT NULL THEN
    RAISE EXCEPTION 'qualified index content immutable' USING ERRCODE='23514';
   END IF; RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF NEW.chunk_set_id IS NULL THEN RETURN NEW; END IF;
 END IF;
 oid:=NEW.org_id;sid:=NEW.snapshot_id;rid:=NEW.repository_id;
 jid:=public.codespy_index_authority(oid,sid);
 SELECT config_hash INTO cfg FROM public.jobs WHERE org_id=oid AND id=jid;
 setid:=CASE WHEN TG_TABLE_NAME='retrieval_chunk_sets' THEN (to_jsonb(NEW)->>'id')::uuid ELSE (to_jsonb(NEW)->>'chunk_set_id')::uuid END;
 IF TG_TABLE_NAME='retrieval_chunk_sets' THEN
  IF NEW.config_hash IS DISTINCT FROM cfg OR NEW.manifest_json->>'config_hash' IS DISTINCT FROM cfg
  OR NEW.manifest_json->>'org_id' IS DISTINCT FROM oid::text OR NEW.manifest_json->>'repository_id' IS DISTINCT FROM rid::text
  OR NEW.manifest_json->>'snapshot_id' IS DISTINCT FROM sid::text
  THEN RAISE EXCEPTION 'index materialization scope differs' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO s FROM public.retrieval_chunk_sets WHERE org_id=oid AND id=setid AND repository_id=rid AND snapshot_id=sid;
  IF s.id IS NULL OR s.config_hash IS DISTINCT FROM cfg THEN RAISE EXCEPTION 'index set scope differs' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM public.index_publication_receipts WHERE org_id=oid AND chunk_set_id=setid)
  THEN RAISE EXCEPTION 'sealed index set cannot be appended' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_TABLE_NAME='source_chunks' THEN
  IF NOT EXISTS(SELECT 1 FROM public.source_files f WHERE f.org_id=oid AND f.snapshot_id=sid AND f.id=NEW.file_id
   AND f.path=NEW.file_path AND f.size_bytes>=NEW.end_byte)
  OR NEW.id<>public.uuid_generate_v5(setid,public.codespy_scan_digest(NEW.provenance_json))
  THEN RAISE EXCEPTION 'index chunk identity differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE TRIGGER guard_embed_spec BEFORE INSERT ON public.embed_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_spec_guard();
CREATE TRIGGER guard_embed_attempt BEFORE INSERT ON public.embed_attempt_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_attempt_guard();
CREATE TRIGGER guard_embed_stage BEFORE INSERT OR DELETE ON public.embed_vector_stage FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_stage_guard();
CREATE TRIGGER guard_embed_receipt BEFORE INSERT ON public.embed_publication_receipts FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_receipt_guard();
CREATE TRIGGER guard_embed_job BEFORE INSERT OR UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_job_guard();
CREATE CONSTRAINT TRIGGER guard_embed_committed AFTER INSERT ON public.search_indices
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.index_type='dense' AND NEW.chunk_set_id IS NOT NULL)
 EXECUTE FUNCTION public.codespy_embed_committed_guard();
CREATE CONSTRAINT TRIGGER guard_embed_committed AFTER INSERT ON public.embed_publication_receipts
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_committed_guard();
CREATE CONSTRAINT TRIGGER guard_embed_committed AFTER INSERT ON public.chunk_embeddings
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.chunk_set_id IS NOT NULL)
 EXECUTE FUNCTION public.codespy_embed_committed_guard();
CREATE CONSTRAINT TRIGGER guard_embed_committed AFTER UPDATE ON public.jobs
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.operation='embed_chunk_set' AND NEW.state IN ('succeeded','partial'))
 EXECUTE FUNCTION public.codespy_embed_committed_guard();
REVOKE ALL ON FUNCTION public.codespy_embed_authority(uuid,uuid),public.codespy_embed_embedding_digest(uuid,uuid),
 public.codespy_embed_spec_guard(),public.codespy_embed_attempt_guard(),public.codespy_embed_stage_guard(),
 public.codespy_embed_receipt_guard(),public.codespy_embed_job_guard(),public.codespy_embed_verify(uuid,uuid),
 public.codespy_embed_committed_guard() FROM PUBLIC;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DROP TRIGGER guard_embed_committed ON public.search_indices;
DROP TRIGGER guard_embed_committed ON public.embed_publication_receipts;
DROP TRIGGER guard_embed_committed ON public.chunk_embeddings;
DROP TRIGGER guard_embed_committed ON public.jobs;
DROP TRIGGER guard_embed_job ON public.jobs;
DROP TRIGGER guard_embed_receipt ON public.embed_publication_receipts;
DROP TRIGGER guard_embed_stage ON public.embed_vector_stage;
DROP TRIGGER guard_embed_attempt ON public.embed_attempt_specs;
DROP TRIGGER guard_embed_spec ON public.embed_specs;
CREATE OR REPLACE FUNCTION public.codespy_index_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; setid uuid; oid uuid; sid uuid; rid uuid; cfg text; s public.retrieval_chunk_sets%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='chunk_embeddings' THEN
  IF TG_OP<>'DELETE' AND (NEW.chunk_set_id IS NOT NULL OR
    EXISTS(SELECT 1 FROM public.source_chunks WHERE org_id=NEW.org_id AND id=NEW.chunk_id AND chunk_set_id IS NOT NULL) OR
    EXISTS(SELECT 1 FROM public.search_indices WHERE org_id=NEW.org_id AND id=NEW.index_id AND chunk_set_id IS NOT NULL))
  THEN RAISE EXCEPTION 'qualified embedding publication unavailable' USING ERRCODE='23514'; END IF;
  IF TG_OP<>'INSERT' AND (OLD.chunk_set_id IS NOT NULL OR
    EXISTS(SELECT 1 FROM public.source_chunks WHERE org_id=OLD.org_id AND id=OLD.chunk_id AND chunk_set_id IS NOT NULL) OR
    EXISTS(SELECT 1 FROM public.search_indices WHERE org_id=OLD.org_id AND id=OLD.index_id AND chunk_set_id IS NOT NULL))
  THEN RAISE EXCEPTION 'qualified embedding mutation unavailable' USING ERRCODE='23514'; END IF;
  RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
 END IF;
 IF TG_TABLE_NAME='source_chunks' OR TG_TABLE_NAME='search_indices' THEN
  IF TG_OP<>'INSERT' THEN
   IF OLD.chunk_set_id IS NOT NULL OR NEW.chunk_set_id IS NOT NULL THEN
    RAISE EXCEPTION 'qualified index content immutable' USING ERRCODE='23514';
   END IF; RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
  END IF;
  IF NEW.chunk_set_id IS NULL THEN RETURN NEW; END IF;
 END IF;
 IF TG_TABLE_NAME='chunk_embeddings' THEN RAISE EXCEPTION 'dense publication unavailable' USING ERRCODE='23514'; END IF;
 oid:=NEW.org_id;sid:=NEW.snapshot_id;rid:=NEW.repository_id;
 jid:=public.codespy_index_authority(oid,sid);
 SELECT config_hash INTO cfg FROM public.jobs WHERE org_id=oid AND id=jid;
 setid:=CASE WHEN TG_TABLE_NAME='retrieval_chunk_sets' THEN (to_jsonb(NEW)->>'id')::uuid ELSE (to_jsonb(NEW)->>'chunk_set_id')::uuid END;
 IF TG_TABLE_NAME='retrieval_chunk_sets' THEN
  IF NEW.config_hash IS DISTINCT FROM cfg OR NEW.manifest_json->>'config_hash' IS DISTINCT FROM cfg
  OR NEW.manifest_json->>'org_id' IS DISTINCT FROM oid::text OR NEW.manifest_json->>'repository_id' IS DISTINCT FROM rid::text
  OR NEW.manifest_json->>'snapshot_id' IS DISTINCT FROM sid::text
  THEN RAISE EXCEPTION 'index materialization scope differs' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO s FROM public.retrieval_chunk_sets WHERE org_id=oid AND id=setid AND repository_id=rid AND snapshot_id=sid;
  IF s.id IS NULL OR s.config_hash IS DISTINCT FROM cfg THEN RAISE EXCEPTION 'index set scope differs' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM public.index_publication_receipts WHERE org_id=oid AND chunk_set_id=setid)
  THEN RAISE EXCEPTION 'sealed index set cannot be appended' USING ERRCODE='23514'; END IF;
 END IF;
 IF TG_TABLE_NAME='source_chunks' THEN
  IF NOT EXISTS(SELECT 1 FROM public.source_files f WHERE f.org_id=oid AND f.snapshot_id=sid AND f.id=NEW.file_id
   AND f.path=NEW.file_path AND f.size_bytes>=NEW.end_byte)
  OR NEW.id<>public.uuid_generate_v5(setid,public.codespy_scan_digest(NEW.provenance_json))
  THEN RAISE EXCEPTION 'index chunk identity differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
DROP FUNCTION public.codespy_embed_committed_guard();
DROP FUNCTION public.codespy_embed_verify(uuid,uuid);
DROP FUNCTION public.codespy_embed_job_guard();
DROP FUNCTION public.codespy_embed_receipt_guard();
DROP FUNCTION public.codespy_embed_stage_guard();
DROP FUNCTION public.codespy_embed_attempt_guard();
DROP FUNCTION public.codespy_embed_spec_guard();
DROP FUNCTION public.codespy_embed_embedding_digest(uuid,uuid);
DROP FUNCTION public.codespy_embed_authority(uuid,uuid);
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
