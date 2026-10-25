"""Scoped immutable lexical materializations and job-specific publication receipts."""

from alembic import op

revision = "0023_index_publication"
down_revision = "0022_triage_commands"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';
CREATE TABLE public.index_specs (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,
 principal_id uuid NOT NULL,config_hash text NOT NULL,spec_hash text NOT NULL,
 config_json jsonb NOT NULL,spec_json jsonb NOT NULL,canonical_text text NOT NULL,
 source_object_id uuid NOT NULL,manifest_object_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 UNIQUE(org_id,repository_id,snapshot_id,id,config_hash,principal_id),
 FOREIGN KEY(org_id,repository_id,snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 FOREIGN KEY(org_id,principal_id) REFERENCES public.memberships(org_id,principal_id),
 FOREIGN KEY(org_id,repository_id,source_object_id) REFERENCES public.object_records(org_id,repository_id,id),
 FOREIGN KEY(org_id,repository_id,manifest_object_id) REFERENCES public.object_records(org_id,repository_id,id),
 CHECK(config_hash ~ '^[0-9a-f]{64}$' AND spec_hash ~ '^[0-9a-f]{64}$'),
 CHECK(canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=spec_json
   AND octet_length(canonical_text)<=98304),
 CHECK(spec_hash=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')),
 CHECK(spec_json->'config'=config_json AND config_hash=public.codespy_scan_digest(config_json)),
 CHECK((spec_json->>'org_id') IS NOT DISTINCT FROM org_id::text AND (spec_json->>'repository_id') IS NOT DISTINCT FROM repository_id::text
   AND (spec_json->>'snapshot_id') IS NOT DISTINCT FROM snapshot_id::text AND (spec_json->>'principal_id') IS NOT DISTINCT FROM principal_id::text)
);
CREATE TABLE public.retrieval_chunk_sets (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,
 config_hash text NOT NULL,materialization_hash text NOT NULL,manifest_hash text NOT NULL,
 manifest_json jsonb NOT NULL,canonical_text text NOT NULL,
 chunk_count integer NOT NULL,safe_bytes bigint NOT NULL,coverage text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),UNIQUE(org_id,repository_id,snapshot_id,config_hash),
 UNIQUE(org_id,repository_id,snapshot_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 CHECK(config_hash ~ '^[0-9a-f]{64}$' AND materialization_hash ~ '^[0-9a-f]{64}$'),
 CHECK(manifest_hash=materialization_hash AND manifest_hash=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')),
 CHECK(canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=manifest_json AND octet_length(canonical_text)<=8388608),
 CHECK(id=public.uuid_generate_v5('ddf1b375-920d-427d-a0cc-706f8f95ad79',materialization_hash)),
 CHECK(chunk_count BETWEEN 0 AND 4096 AND safe_bytes BETWEEN 0 AND 16777216),
 CHECK(coverage IN ('complete','partial'))
);
ALTER TABLE public.source_chunks ADD COLUMN chunk_set_id uuid,
 ADD COLUMN safe_content_sha256 text,ADD COLUMN safe_start_byte bigint,
 ADD COLUMN safe_end_byte bigint,ADD COLUMN provenance_json jsonb;
ALTER TABLE public.source_chunks DROP CONSTRAINT uq_chunk_scope;
CREATE UNIQUE INDEX uq_chunk_legacy ON public.source_chunks(org_id,snapshot_id,file_id,chunk_index,chunker_version) WHERE chunk_set_id IS NULL;
CREATE UNIQUE INDEX uq_chunk_materialized ON public.source_chunks(org_id,chunk_set_id,file_id,chunk_index) WHERE chunk_set_id IS NOT NULL;
ALTER TABLE public.source_chunks ADD CONSTRAINT uq_chunk_set_scope UNIQUE(org_id,repository_id,snapshot_id,id,chunk_set_id),
 ADD CONSTRAINT fk_chunk_set FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_set_id) REFERENCES public.retrieval_chunk_sets(org_id,repository_id,snapshot_id,id),
 ADD CONSTRAINT ck_chunk_materialization CHECK (
 (chunk_set_id IS NULL AND safe_content_sha256 IS NULL AND safe_start_byte IS NULL AND safe_end_byte IS NULL AND provenance_json IS NULL) OR
 (chunk_set_id IS NOT NULL AND safe_content_sha256 IS NOT NULL AND safe_start_byte IS NOT NULL AND safe_end_byte IS NOT NULL AND provenance_json IS NOT NULL
 AND safe_content_sha256=encode(sha256(convert_to(safe_content,'UTF8')),'hex')
 AND safe_start_byte>=0 AND safe_end_byte-safe_start_byte=octet_length(safe_content)
 AND byte_count=end_byte-start_byte AND end_byte>start_byte
 AND octet_length(safe_content) BETWEEN 1 AND 4096 AND estimated_tokens BETWEEN 1 AND 1024
 AND jsonb_typeof(provenance_json)='object' AND octet_length(provenance_json::text)<=65536));
ALTER TABLE public.search_indices ADD COLUMN chunk_set_id uuid,
 ADD CONSTRAINT fk_index_set FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_set_id) REFERENCES public.retrieval_chunk_sets(org_id,repository_id,snapshot_id,id),
 ADD CONSTRAINT uq_index_result_scope UNIQUE(org_id,repository_id,snapshot_id,id),
 ADD CONSTRAINT uq_index_set_scope UNIQUE(org_id,repository_id,snapshot_id,id,chunk_set_id),
 ADD CONSTRAINT ck_index_qualified CHECK(chunk_set_id IS NULL OR
 (index_type='lexical' AND state IN ('ready','partial_ready') AND sealed_at IS NOT NULL
 AND embedding_provider IS NULL AND embedding_model IS NULL AND dimension IS NULL
 AND id=public.uuid_generate_v5(chunk_set_id,'lexical-v1')));
CREATE UNIQUE INDEX uq_index_set_kind ON public.search_indices(org_id,chunk_set_id,index_type) WHERE chunk_set_id IS NOT NULL;
ALTER TABLE public.chunk_embeddings ADD COLUMN chunk_set_id uuid,
 ADD CONSTRAINT fk_embedding_chunk_set FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_id,chunk_set_id)
 REFERENCES public.source_chunks(org_id,repository_id,snapshot_id,id,chunk_set_id),
 ADD CONSTRAINT fk_embedding_index_set FOREIGN KEY(org_id,repository_id,snapshot_id,index_id,chunk_set_id)
 REFERENCES public.search_indices(org_id,repository_id,snapshot_id,id,chunk_set_id);
ALTER TABLE public.jobs ADD COLUMN index_spec_id uuid,ADD COLUMN result_index_id uuid;
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot','analyze_structure','scan_snapshot','index_snapshot')),
 ADD CONSTRAINT ck_job_index CHECK(
 (operation='index_snapshot' AND index_spec_id IS NOT NULL AND snapshot_id IS NOT NULL
 AND submission_id IS NULL AND parent_job_id IS NULL AND analysis_spec_id IS NULL
 AND scan_spec_id IS NULL AND result_run_id IS NULL) OR
 (operation<>'index_snapshot' AND index_spec_id IS NULL AND result_index_id IS NULL)),
 ADD CONSTRAINT fk_job_index_spec FOREIGN KEY(org_id,repository_id,snapshot_id,index_spec_id,config_hash,principal_id)
 REFERENCES public.index_specs(org_id,repository_id,snapshot_id,id,config_hash,principal_id),
 ADD CONSTRAINT fk_job_result_index FOREIGN KEY(org_id,repository_id,snapshot_id,result_index_id) REFERENCES public.search_indices(org_id,repository_id,snapshot_id,id);
CREATE TABLE public.index_attempt_specs (
 org_id uuid NOT NULL,id uuid NOT NULL,job_id uuid NOT NULL,fence bigint NOT NULL,
 spec_id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,spec_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),UNIQUE(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,job_id,id,fence) REFERENCES public.job_attempts(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.index_specs(org_id,id),
 CHECK(fence>0 AND spec_hash ~ '^[0-9a-f]{64}$')
);
CREATE TABLE public.index_publication_receipts (
 org_id uuid NOT NULL,job_id uuid NOT NULL,attempt_id uuid NOT NULL,fence bigint NOT NULL,
 repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,spec_id uuid NOT NULL,spec_hash text NOT NULL,
 chunk_set_id uuid NOT NULL,index_id uuid NOT NULL,draft_digest text NOT NULL,
 materialization_hash text NOT NULL,coverage text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,job_id,attempt_id,fence),UNIQUE(org_id,job_id),
 FOREIGN KEY(org_id,job_id,attempt_id,fence) REFERENCES public.index_attempt_specs(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.index_specs(org_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_set_id) REFERENCES public.retrieval_chunk_sets(org_id,repository_id,snapshot_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,index_id,chunk_set_id) REFERENCES public.search_indices(org_id,repository_id,snapshot_id,id,chunk_set_id),
 CHECK(fence>0 AND spec_hash ~ '^[0-9a-f]{64}$' AND draft_digest ~ '^[0-9a-f]{64}$' AND materialization_hash ~ '^[0-9a-f]{64}$'),
 CHECK(coverage IN ('complete','partial'))
);
CREATE INDEX ix_index_spec_history ON public.index_specs(org_id,repository_id,snapshot_id,created_at,id);
CREATE INDEX ix_index_receipt_job ON public.index_publication_receipts(org_id,job_id);
"""

GUARDS = r"""
CREATE FUNCTION public.codespy_index_spec_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE src public.object_records%ROWTYPE; man public.object_records%ROWTYPE;
BEGIN
 IF NEW.config_json->'privacy_policy' IS DISTINCT FROM '{"allow_cloud_providers": false, "allow_original_view": false, "max_view_bytes": 1048576, "policy_version": "v1_default", "redact_secrets": true, "replacement_template": "[REDACTED_SECRET:{secret_type}]", "sensitive_path_patterns": [".env*", "*.pem", "*.key", "*.pkcs12", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "id_ecdsa*", "*credentials*", "*service_account*.json", ".aws/*", ".ssh/*"]}'::jsonb
 OR NEW.config_json->>'chunker_version' IS DISTINCT FROM 'v1_safe_windows'
 OR NEW.config_json->>'lexical_version' IS DISTINCT FROM 'pg-simple-weighted-v1'
 OR NEW.config_json->>'selection_policy' IS DISTINCT FROM 'admitted-path-order-v1'
 OR NEW.config_json->>'tokenizer_name' IS DISTINCT FROM 'cl100k_base'
 OR NEW.config_json->>'tokenizer_sha256' IS DISTINCT FROM '223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7'
 OR NEW.config_json->'target_tokens' IS DISTINCT FROM '768'::jsonb
 OR NEW.config_json->'max_tokens' IS DISTINCT FROM '1024'::jsonb
 OR NEW.config_json->'max_bytes' IS DISTINCT FROM '4096'::jsonb
 OR NEW.config_json->'overlap_tokens' IS DISTINCT FROM '64'::jsonb
 OR NEW.config_json->'max_chunks' IS DISTINCT FROM '4096'::jsonb
 OR coalesce(NEW.config_json->>'runtime_image','') !~ '^sha256:[0-9a-f]{64}$'
 OR coalesce(NEW.config_json->>'runtime_source_sha256','') !~ '^[0-9a-f]{64}$'
 THEN RAISE EXCEPTION 'unqualified index profile' USING ERRCODE='23514'; END IF;
 SELECT * INTO src FROM public.object_records WHERE org_id=NEW.org_id AND id=NEW.source_object_id;
 SELECT * INTO man FROM public.object_records WHERE org_id=NEW.org_id AND id=NEW.manifest_object_id;
 IF src.state IS DISTINCT FROM 'available' OR src.kind IS DISTINCT FROM 'source'
 OR man.state IS DISTINCT FROM 'available' OR man.kind IS DISTINCT FROM 'manifest'
 OR (src.expires_at IS NOT NULL AND src.expires_at<=clock_timestamp())
 OR (man.expires_at IS NOT NULL AND man.expires_at<=clock_timestamp())
 OR ROW(src.key,src.sha256,src.size_bytes) IS DISTINCT FROM ROW(NEW.spec_json->'source'->>'key',NEW.spec_json->'source'->>'sha256',(NEW.spec_json->'source'->>'size_bytes')::bigint)
 OR ROW(man.key,man.sha256,man.size_bytes) IS DISTINCT FROM ROW(NEW.spec_json->'manifest'->>'key',NEW.spec_json->'manifest'->>'sha256',(NEW.spec_json->'manifest'->>'size_bytes')::bigint)
 OR src.version_id IS DISTINCT FROM NEW.spec_json->'source'->>'version_id'
 OR man.version_id IS DISTINCT FROM NEW.spec_json->'manifest'->>'version_id'
 OR NOT EXISTS(SELECT 1 FROM public.source_snapshots s JOIN public.artifacts a ON a.org_id=s.org_id AND a.id=s.artifact_id
   JOIN public.jobs j ON j.org_id=s.org_id AND j.snapshot_id=s.id AND j.repository_id=s.repository_id
   JOIN public.source_submissions u ON u.org_id=j.org_id AND u.id=j.submission_id
   WHERE s.org_id=NEW.org_id AND s.repository_id=NEW.repository_id AND s.id=NEW.snapshot_id AND s.state='sealed'
   AND a.storage_key=src.key AND a.sha256=src.sha256 AND a.size_bytes=src.size_bytes
   AND j.operation='admit_source' AND j.state='succeeded' AND j.result_object_id=man.id
   AND u.state='admitted' AND u.snapshot_id=s.id AND u.actual_sha=a.sha256 AND u.policy_hash=s.admission_config_hash)
 THEN RAISE EXCEPTION 'index admitted source unavailable' USING ERRCODE='23514'; END IF;
 IF jsonb_typeof(NEW.spec_json->'files') IS DISTINCT FROM 'array'
 OR jsonb_array_length(NEW.spec_json->'files')>128
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.spec_json->'files') f
  WHERE NOT EXISTS(SELECT 1 FROM public.source_files sf JOIN public.inventory_entries e
   ON e.org_id=sf.org_id AND e.snapshot_id=sf.snapshot_id AND e.file_id=sf.id
   WHERE sf.org_id=NEW.org_id AND sf.snapshot_id=NEW.snapshot_id AND sf.id=(f->>'file_id')::uuid
   AND sf.path=f->>'path' AND sf.sha256=f->>'sha256' AND sf.size_bytes=(f->>'size_bytes')::bigint AND e.eligible))
 THEN RAISE EXCEPTION 'index file selection differs' USING ERRCODE='23514'; END IF;
 IF (SELECT count(*) FROM jsonb_array_elements(NEW.spec_json->'files')) <>
    (SELECT count(DISTINCT f->>'file_id') FROM jsonb_array_elements(NEW.spec_json->'files') f)
 OR (SELECT count(*) FROM jsonb_array_elements(NEW.spec_json->'files')) <>
    (SELECT count(DISTINCT f->>'path') FROM jsonb_array_elements(NEW.spec_json->'files') f)
 OR coalesce((SELECT sum((f->>'size_bytes')::bigint) FROM jsonb_array_elements(NEW.spec_json->'files') f),0)>16777216
 OR jsonb_typeof(NEW.spec_json->'selection_omissions') IS DISTINCT FROM 'array'
 OR (NEW.spec_json->>'admitted_files')::integer IS DISTINCT FROM
    (SELECT count(*)::integer FROM public.source_files WHERE org_id=NEW.org_id AND snapshot_id=NEW.snapshot_id)
 THEN RAISE EXCEPTION 'invalid index selection coverage' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_index_authority(oid uuid,sid uuid) RETURNS uuid LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; s public.index_specs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=oid AND id=jid;
 SELECT * INTO s FROM public.index_specs WHERE org_id=oid AND id=j.index_spec_id;
 IF j.operation IS DISTINCT FROM 'index_snapshot' OR j.snapshot_id IS DISTINCT FROM sid OR s.id IS NULL
 OR s.principal_id IS DISTINCT FROM j.principal_id OR s.config_hash IS DISTINCT FROM j.config_hash
 OR NOT EXISTS(SELECT 1 FROM public.index_attempt_specs a WHERE a.org_id=oid AND a.job_id=jid
  AND a.id=j.current_attempt_id AND a.fence=j.fence AND a.spec_id=s.id AND a.spec_hash=s.spec_hash)
 THEN RAISE EXCEPTION 'index worker binding differs' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.object_records o WHERE o.org_id=oid AND o.id IN (s.source_object_id,s.manifest_object_id)
  AND (o.state<>'available' OR (o.expires_at IS NOT NULL AND o.expires_at<=clock_timestamp())))
 THEN RAISE EXCEPTION 'index retained source unavailable' USING ERRCODE='23514'; END IF;
 RETURN jid;
END $$;

CREATE FUNCTION public.codespy_index_attempt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; s public.index_specs%ROWTYPE;
BEGIN
 jid:=public.codespy_phase2_authority();
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO s FROM public.index_specs WHERE org_id=NEW.org_id AND id=j.index_spec_id;
 IF NEW.job_id IS DISTINCT FROM jid OR NEW.id IS DISTINCT FROM j.current_attempt_id OR NEW.fence IS DISTINCT FROM j.fence
 OR j.operation IS DISTINCT FROM 'index_snapshot' OR s.id IS NULL OR NEW.spec_id IS DISTINCT FROM s.id
 OR NEW.spec_hash IS DISTINCT FROM s.spec_hash OR NEW.repository_id IS DISTINCT FROM j.repository_id OR NEW.snapshot_id IS DISTINCT FROM j.snapshot_id
 THEN RAISE EXCEPTION 'index attempt differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_index_result_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
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

CREATE FUNCTION public.codespy_index_complete_set(oid uuid,setid uuid) RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE s public.retrieval_chunk_sets%ROWTYPE; item jsonb; n bigint; total bigint; c public.source_chunks%ROWTYPE;
BEGIN
 SELECT * INTO s FROM public.retrieval_chunk_sets WHERE org_id=oid AND id=setid;
 IF s.id IS NULL OR jsonb_typeof(s.manifest_json->'chunks') IS DISTINCT FROM 'array'
 OR public.codespy_scan_digest(s.manifest_json)<>s.materialization_hash
 THEN RAISE EXCEPTION 'index manifest differs' USING ERRCODE='23514'; END IF;
 IF jsonb_typeof(s.manifest_json->'coverage') IS DISTINCT FROM 'object'
 OR jsonb_typeof(s.manifest_json->'coverage'->'omissions') IS DISTINCT FROM 'array'
 OR jsonb_typeof(s.manifest_json->'processed_file_ids') IS DISTINCT FROM 'array'
 OR (s.coverage='complete') IS DISTINCT FROM (jsonb_array_length(s.manifest_json->'coverage'->'omissions')=0)
 OR (s.manifest_json->'coverage'->>'processed_files')::integer IS DISTINCT FROM jsonb_array_length(s.manifest_json->'processed_file_ids')
 OR coalesce((s.manifest_json->'coverage'->>'selected_files')::integer,-1) NOT BETWEEN jsonb_array_length(s.manifest_json->'processed_file_ids') AND 128
 OR coalesce((s.manifest_json->'coverage'->>'admitted_files')::integer,-1) NOT BETWEEN (s.manifest_json->'coverage'->>'selected_files')::integer AND 10000
 OR (SELECT count(DISTINCT (v->>'file_id',v->>'chunk_index')) FROM jsonb_array_elements(s.manifest_json->'chunks') v)<>jsonb_array_length(s.manifest_json->'chunks')
 OR (SELECT count(DISTINCT v) FROM jsonb_array_elements_text(s.manifest_json->'processed_file_ids') v)<>jsonb_array_length(s.manifest_json->'processed_file_ids')
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(s.manifest_json->'coverage'->'omissions') v WHERE
    coalesce(v->>'unit','') NOT IN ('file','chunk') OR coalesce(v->>'reason','') NOT IN ('source_limit','sensitive_path','unsupported_encoding','chunk_limit','safe_byte_limit','manifest_limit','draft_limit','unmappable_range','placeholder_limit')
    OR jsonb_typeof(v->'count') IS DISTINCT FROM 'number' OR (v->>'count')::bigint NOT BETWEEN 1 AND 1000000)
 OR (SELECT count(DISTINCT (v->>'reason',v->>'unit')) FROM jsonb_array_elements(s.manifest_json->'coverage'->'omissions') v)<>jsonb_array_length(s.manifest_json->'coverage'->'omissions')
 OR coalesce((SELECT sum((v->>'count')::bigint) FROM jsonb_array_elements(s.manifest_json->'coverage'->'omissions') v WHERE v->>'unit'='file'),0)
    <> (s.manifest_json->'coverage'->>'admitted_files')::integer-(s.manifest_json->'coverage'->>'processed_files')::integer
 THEN RAISE EXCEPTION 'index manifest coverage or uniqueness differs' USING ERRCODE='23514'; END IF;
 SELECT count(*),coalesce(sum(octet_length(safe_content)),0) INTO n,total FROM public.source_chunks WHERE org_id=oid AND chunk_set_id=setid;
 IF n<>s.chunk_count OR total<>s.safe_bytes OR jsonb_array_length(s.manifest_json->'chunks')<>n
 THEN RAISE EXCEPTION 'index content count differs' USING ERRCODE='23514'; END IF;
 FOR item IN SELECT * FROM jsonb_array_elements(s.manifest_json->'chunks') LOOP
  SELECT * INTO c FROM public.source_chunks WHERE org_id=oid AND chunk_set_id=setid AND file_id=(item->>'file_id')::uuid AND chunk_index=(item->>'chunk_index')::integer;
  IF c.id IS NULL OR c.provenance_json IS DISTINCT FROM item
   OR ROW(c.repository_id,c.snapshot_id,c.file_path,c.chunk_kind,c.start_byte,c.end_byte,c.start_line,c.end_line,c.safe_start_byte,c.safe_end_byte,c.content_sha256,c.safe_content_sha256,c.estimated_tokens,c.redaction_digest)
   IS DISTINCT FROM ROW(s.repository_id,s.snapshot_id,item->>'file_path',item->>'chunk_kind',(item->>'start_byte')::bigint,(item->>'end_byte')::bigint,(item->>'start_line')::integer,(item->>'end_line')::integer,(item->>'safe_start_byte')::bigint,(item->>'safe_end_byte')::bigint,item->>'content_sha256',item->>'safe_content_sha256',(item->>'token_count')::integer,item->>'redaction_digest')
  THEN RAISE EXCEPTION 'index stored chunk differs' USING ERRCODE='23514'; END IF;
 END LOOP;
END $$;

CREATE FUNCTION public.codespy_index_job_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE r public.index_publication_receipts%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.operation='index_snapshot' OR NEW.operation='index_snapshot') AND
 ROW(OLD.org_id,OLD.id,OLD.repository_id,OLD.snapshot_id,OLD.principal_id,OLD.index_spec_id,OLD.config_hash,OLD.operation,OLD.request_key,OLD.request_hash,OLD.retry_of_job_id)
 IS DISTINCT FROM ROW(NEW.org_id,NEW.id,NEW.repository_id,NEW.snapshot_id,NEW.principal_id,NEW.index_spec_id,NEW.config_hash,NEW.operation,NEW.request_key,NEW.request_hash,NEW.retry_of_job_id)
 THEN RAISE EXCEPTION 'index job identity immutable' USING ERRCODE='23514'; END IF;
 IF NEW.operation='index_snapshot' AND NEW.state IN ('succeeded','partial') THEN
  SELECT * INTO r FROM public.index_publication_receipts WHERE org_id=NEW.org_id AND job_id=NEW.id AND attempt_id=NEW.current_attempt_id AND fence=NEW.fence;
  IF r.job_id IS NULL OR NEW.result_index_id IS DISTINCT FROM r.index_id
   OR (NEW.state='succeeded') IS DISTINCT FROM (r.coverage='complete')
  THEN RAISE EXCEPTION 'index terminal receipt differs' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_index_receipt_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE jid uuid; j public.jobs%ROWTYPE; a public.index_attempt_specs%ROWTYPE; s public.retrieval_chunk_sets%ROWTYPE;
BEGIN
 jid:=public.codespy_index_authority(NEW.org_id,NEW.snapshot_id);
 SELECT * INTO j FROM public.jobs WHERE org_id=NEW.org_id AND id=jid;
 SELECT * INTO a FROM public.index_attempt_specs WHERE org_id=NEW.org_id AND id=j.current_attempt_id;
 SELECT * INTO s FROM public.retrieval_chunk_sets WHERE org_id=NEW.org_id AND id=NEW.chunk_set_id;
 IF NEW.job_id IS DISTINCT FROM jid OR NEW.attempt_id IS DISTINCT FROM j.current_attempt_id OR NEW.fence IS DISTINCT FROM j.fence
 OR NEW.repository_id IS DISTINCT FROM j.repository_id OR NEW.spec_id IS DISTINCT FROM j.index_spec_id OR NEW.spec_hash IS DISTINCT FROM a.spec_hash
 OR NEW.materialization_hash IS DISTINCT FROM s.materialization_hash OR NEW.coverage IS DISTINCT FROM s.coverage
 OR s.config_hash IS DISTINCT FROM j.config_hash
 THEN RAISE EXCEPTION 'index receipt binding differs' USING ERRCODE='23514'; END IF;
 PERFORM public.codespy_index_complete_set(NEW.org_id,NEW.chunk_set_id);
 IF (s.manifest_json->'coverage'->>'selected_files')::integer IS DISTINCT FROM
      (SELECT jsonb_array_length(spec_json->'files') FROM public.index_specs WHERE org_id=NEW.org_id AND id=NEW.spec_id)
 OR (s.manifest_json->'coverage'->>'admitted_files')::integer IS DISTINCT FROM
      (SELECT (spec_json->>'admitted_files')::integer FROM public.index_specs WHERE org_id=NEW.org_id AND id=NEW.spec_id)
 OR EXISTS(SELECT 1 FROM jsonb_array_elements_text(s.manifest_json->'processed_file_ids') v
    WHERE NOT EXISTS(SELECT 1 FROM public.index_specs sp,jsonb_array_elements(sp.spec_json->'files') f
      WHERE sp.org_id=NEW.org_id AND sp.id=NEW.spec_id AND f->>'file_id'=v))
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(s.manifest_json->'chunks') c
    WHERE NOT EXISTS(SELECT 1 FROM jsonb_array_elements(s.manifest_json->'processed_file_ids') f WHERE f=c->'file_id'))
 THEN RAISE EXCEPTION 'index receipt selection differs' USING ERRCODE='23514'; END IF;
 IF NEW.draft_digest IS DISTINCT FROM public.codespy_scan_digest(
   s.manifest_json || jsonb_build_object('schema_version',1,'spec_hash',NEW.spec_hash,'chunks',
     coalesce((SELECT jsonb_agg(c.provenance_json || jsonb_build_object('safe_content',c.safe_content) ORDER BY v.ordinality)
       FROM jsonb_array_elements(s.manifest_json->'chunks') WITH ORDINALITY v(item,ordinality)
       JOIN public.source_chunks c ON c.org_id=NEW.org_id AND c.chunk_set_id=NEW.chunk_set_id
         AND c.file_id=(v.item->>'file_id')::uuid AND c.chunk_index=(v.item->>'chunk_index')::integer),'[]'::jsonb)))
 THEN RAISE EXCEPTION 'index receipt draft digest differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;

CREATE FUNCTION public.codespy_index_committed_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE oid uuid;setid uuid;r public.index_publication_receipts%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='jobs' THEN
  IF NEW.operation<>'index_snapshot' OR NEW.state NOT IN ('succeeded','partial') THEN RETURN NULL; END IF;
  SELECT * INTO r FROM public.index_publication_receipts WHERE org_id=NEW.org_id AND job_id=NEW.id;
  oid:=NEW.org_id;setid:=r.chunk_set_id;
 ELSE
  oid:=NEW.org_id;
  IF TG_TABLE_NAME='retrieval_chunk_sets' THEN setid:=NEW.id; ELSE setid:=NEW.chunk_set_id; END IF;
  IF setid IS NULL THEN RETURN NULL; END IF;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.index_publication_receipts p JOIN public.jobs j ON j.org_id=p.org_id AND j.id=p.job_id
  JOIN public.job_attempts a ON a.org_id=j.org_id AND a.job_id=j.id AND a.id=j.current_attempt_id
  JOIN public.retrieval_chunk_sets s ON s.org_id=p.org_id AND s.id=p.chunk_set_id
  JOIN public.search_indices i ON i.org_id=p.org_id AND i.id=p.index_id AND i.chunk_set_id=p.chunk_set_id
  WHERE p.org_id=oid AND p.chunk_set_id=setid AND j.state IN ('succeeded','partial') AND j.result_index_id=p.index_id
  AND (TG_TABLE_NAME NOT IN ('jobs','index_publication_receipts') OR p.job_id=(CASE WHEN TG_TABLE_NAME='jobs' THEN (to_jsonb(NEW)->>'id')::uuid ELSE (to_jsonb(NEW)->>'job_id')::uuid END))
  AND j.current_attempt_id=p.attempt_id AND j.fence=p.fence AND a.state='succeeded' AND j.publication_checked_at IS NOT NULL
  AND (j.state='succeeded')=(s.coverage='complete') AND p.coverage=s.coverage
  AND i.state=CASE WHEN s.coverage='complete' THEN 'ready' ELSE 'partial_ready' END
  AND i.total_chunks=s.chunk_count AND i.indexed_chunks=s.chunk_count AND i.failed_chunks=0)
 THEN RAISE EXCEPTION 'uncommitted index publication' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME IN ('retrieval_chunk_sets','jobs') THEN
  PERFORM public.codespy_index_complete_set(oid,setid);
 END IF;
 RETURN NULL;
END $$;
CREATE TRIGGER guard_index_spec BEFORE INSERT ON public.index_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_index_spec_guard();
CREATE TRIGGER guard_index_attempt BEFORE INSERT ON public.index_attempt_specs FOR EACH ROW EXECUTE FUNCTION public.codespy_index_attempt_guard();
CREATE TRIGGER guard_index_job BEFORE INSERT OR UPDATE ON public.jobs FOR EACH ROW EXECUTE FUNCTION public.codespy_index_job_guard();
CREATE TRIGGER guard_index_receipt BEFORE INSERT ON public.index_publication_receipts FOR EACH ROW EXECUTE FUNCTION public.codespy_index_receipt_guard();
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['index_specs','index_attempt_specs','retrieval_chunk_sets','index_publication_receipts'] LOOP
  EXECUTE format('CREATE TRIGGER guard_index_immutable BEFORE UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation()',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['retrieval_chunk_sets','source_chunks','search_indices','chunk_embeddings'] LOOP
  EXECUTE format('CREATE TRIGGER guard_index_result BEFORE INSERT OR UPDATE OR DELETE ON public.%I FOR EACH ROW EXECUTE FUNCTION public.codespy_index_result_guard()',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['retrieval_chunk_sets','source_chunks','search_indices','index_publication_receipts'] LOOP
  EXECUTE format('CREATE CONSTRAINT TRIGGER guard_index_committed AFTER INSERT ON public.%I DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_index_committed_guard()',t);
 END LOOP;
END $$;
CREATE CONSTRAINT TRIGGER guard_index_job_committed AFTER UPDATE ON public.jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_index_committed_guard();
REVOKE ALL ON FUNCTION public.codespy_index_spec_guard(),public.codespy_index_authority(uuid,uuid),public.codespy_index_attempt_guard(),public.codespy_index_result_guard(),public.codespy_index_complete_set(uuid,uuid),public.codespy_index_job_guard(),public.codespy_index_receipt_guard(),public.codespy_index_committed_guard() FROM PUBLIC;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.index_specs) OR EXISTS(SELECT 1 FROM public.retrieval_chunk_sets)
 OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='index_snapshot')
 THEN RAISE EXCEPTION 'index history must be preserved'; END IF;
END $$;
DROP TRIGGER guard_index_job ON public.jobs;
DROP TRIGGER guard_index_job_committed ON public.jobs;
DO $$ DECLARE t text;BEGIN
 FOREACH t IN ARRAY ARRAY['source_chunks','search_indices','chunk_embeddings'] LOOP
  EXECUTE format('DROP TRIGGER guard_index_result ON public.%I',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['source_chunks','search_indices'] LOOP
  EXECUTE format('DROP TRIGGER guard_index_committed ON public.%I',t);
 END LOOP;
END $$;
DROP TABLE public.index_publication_receipts,public.index_attempt_specs;
ALTER TABLE public.jobs DROP CONSTRAINT fk_job_index_spec,DROP CONSTRAINT fk_job_result_index,DROP CONSTRAINT ck_job_index,DROP CONSTRAINT ck_job_operation,
 DROP COLUMN index_spec_id,DROP COLUMN result_index_id;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot','analyze_structure','scan_snapshot'));
ALTER TABLE public.chunk_embeddings DROP CONSTRAINT fk_embedding_chunk_set,DROP CONSTRAINT fk_embedding_index_set,DROP COLUMN chunk_set_id;
ALTER TABLE public.search_indices DROP CONSTRAINT fk_index_set,DROP CONSTRAINT uq_index_set_scope,DROP CONSTRAINT uq_index_result_scope,DROP CONSTRAINT ck_index_qualified,DROP COLUMN chunk_set_id;
ALTER TABLE public.source_chunks DROP CONSTRAINT fk_chunk_set,DROP CONSTRAINT uq_chunk_set_scope,DROP CONSTRAINT ck_chunk_materialization,
 DROP COLUMN chunk_set_id,DROP COLUMN safe_content_sha256,DROP COLUMN safe_start_byte,DROP COLUMN safe_end_byte,DROP COLUMN provenance_json;
ALTER TABLE public.source_chunks ADD CONSTRAINT uq_chunk_scope UNIQUE(org_id,snapshot_id,file_id,chunk_index,chunker_version);
DROP TABLE public.retrieval_chunk_sets,public.index_specs;
DROP FUNCTION public.codespy_index_spec_guard(),public.codespy_index_authority(uuid,uuid),public.codespy_index_attempt_guard(),public.codespy_index_result_guard(),public.codespy_index_complete_set(uuid,uuid),public.codespy_index_job_guard(),public.codespy_index_receipt_guard(),public.codespy_index_committed_guard();
"""


def upgrade() -> None:
    op.execute(SQL)
    op.execute(GUARDS)


def downgrade() -> None:
    op.execute(DOWN)
