# ruff: noqa: E501
"""Schema for fenced dense index publication: specs, attempts, receipts, staged vectors.

Only structure and constraints. Insert authority, accounting functions and role grants arrive in
later revisions, so until then nothing but the migration role can write the new tables.
"""

from alembic import op

revision = "0044_dense_index_schema"
down_revision = "0043_ast_file_facts"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.embed_specs (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,
 principal_id uuid NOT NULL,chunk_set_id uuid NOT NULL,lexical_index_id uuid NOT NULL,
 provider text NOT NULL,model_id text NOT NULL,dimension integer NOT NULL,normalize boolean NOT NULL,
 price_version text NOT NULL,config_hash text NOT NULL,spec_hash text NOT NULL,
 config_json jsonb NOT NULL,spec_json jsonb NOT NULL,canonical_text text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 UNIQUE(org_id,repository_id,snapshot_id,id,config_hash,principal_id),
 UNIQUE(org_id,chunk_set_id,provider,model_id,dimension,normalize,price_version),
 FOREIGN KEY(org_id,repository_id,snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 FOREIGN KEY(org_id,principal_id) REFERENCES public.memberships(org_id,principal_id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_set_id) REFERENCES public.retrieval_chunk_sets(org_id,repository_id,snapshot_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,lexical_index_id,chunk_set_id) REFERENCES public.search_indices(org_id,repository_id,snapshot_id,id,chunk_set_id),
 CHECK(config_hash ~ '^[0-9a-f]{64}$' AND spec_hash ~ '^[0-9a-f]{64}$'),
 CHECK(octet_length(provider) BETWEEN 1 AND 128 AND octet_length(model_id) BETWEEN 1 AND 128 AND octet_length(price_version) BETWEEN 1 AND 128),
 CHECK(dimension IN (256,512,1024)),
 CHECK(lexical_index_id=public.uuid_generate_v5(chunk_set_id,'lexical-v1')),
 CHECK(canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=spec_json AND octet_length(canonical_text)<=65536),
 CHECK(spec_hash=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')),
 CHECK(spec_json->'config'=config_json AND config_hash=public.codespy_scan_digest(config_json)),
 CHECK((spec_json->>'org_id') IS NOT DISTINCT FROM org_id::text AND (spec_json->>'repository_id') IS NOT DISTINCT FROM repository_id::text
   AND (spec_json->>'snapshot_id') IS NOT DISTINCT FROM snapshot_id::text AND (spec_json->>'principal_id') IS NOT DISTINCT FROM principal_id::text
   AND (spec_json->>'chunk_set_id') IS NOT DISTINCT FROM chunk_set_id::text)
);
CREATE INDEX ix_embed_spec_history ON public.embed_specs(org_id,repository_id,snapshot_id,created_at,id);

ALTER TABLE public.jobs ADD COLUMN embed_spec_id uuid,
 ADD CONSTRAINT fk_job_embed_spec FOREIGN KEY(org_id,repository_id,snapshot_id,embed_spec_id,config_hash,principal_id)
 REFERENCES public.embed_specs(org_id,repository_id,snapshot_id,id,config_hash,principal_id);
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN
 ('admit_source','inventory_snapshot','analyze_structure','scan_snapshot','index_snapshot','investigate_snapshot','embed_chunk_set'));
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_index;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_index CHECK(
 (operation='index_snapshot' AND index_spec_id IS NOT NULL AND snapshot_id IS NOT NULL
  AND submission_id IS NULL AND parent_job_id IS NULL AND analysis_spec_id IS NULL
  AND scan_spec_id IS NULL AND result_run_id IS NULL) OR
 (operation<>'index_snapshot' AND index_spec_id IS NULL AND (operation='embed_chunk_set' OR result_index_id IS NULL)));
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_embed CHECK(
 (operation='embed_chunk_set' AND embed_spec_id IS NOT NULL AND snapshot_id IS NOT NULL
  AND submission_id IS NULL AND parent_job_id IS NULL AND analysis_spec_id IS NULL AND scan_spec_id IS NULL
  AND index_spec_id IS NULL AND result_run_id IS NULL AND result_object_id IS NULL
  AND investigation_id IS NULL AND spec_hash IS NULL AND result_report_id IS NULL) OR
 (operation<>'embed_chunk_set' AND embed_spec_id IS NULL));

ALTER TABLE public.search_indices DROP CONSTRAINT ck_index_qualified;
ALTER TABLE public.search_indices ADD CONSTRAINT ck_index_qualified CHECK(chunk_set_id IS NULL OR
 (index_type='lexical' AND state IN ('ready','partial_ready') AND sealed_at IS NOT NULL
  AND embedding_provider IS NULL AND embedding_model IS NULL AND dimension IS NULL
  AND id=public.uuid_generate_v5(chunk_set_id,'lexical-v1')) OR
 (index_type='dense' AND state IN ('ready','partial_ready') AND sealed_at IS NOT NULL
  AND embedding_provider IS NOT NULL AND embedding_model IS NOT NULL AND dimension IN (256,512,1024)
  AND id=public.uuid_generate_v5(chunk_set_id,'dense-v1:'||embedding_provider||':'||embedding_model||':'||dimension::text)));
DROP INDEX public.uq_index_set_kind;
CREATE UNIQUE INDEX uq_index_set_lexical ON public.search_indices(org_id,chunk_set_id)
 WHERE chunk_set_id IS NOT NULL AND index_type='lexical';
CREATE UNIQUE INDEX uq_index_set_dense ON public.search_indices(org_id,chunk_set_id,embedding_provider,embedding_model,dimension)
 WHERE chunk_set_id IS NOT NULL AND index_type='dense';

CREATE TABLE public.embed_attempt_specs (
 org_id uuid NOT NULL,id uuid NOT NULL,job_id uuid NOT NULL,fence bigint NOT NULL,
 spec_id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,spec_hash text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),UNIQUE(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,job_id,id,fence) REFERENCES public.job_attempts(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.embed_specs(org_id,id),
 CHECK(fence>0 AND spec_hash ~ '^[0-9a-f]{64}$')
);
CREATE TABLE public.embed_publication_receipts (
 org_id uuid NOT NULL,job_id uuid NOT NULL,attempt_id uuid NOT NULL,fence bigint NOT NULL,
 repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,spec_id uuid NOT NULL,spec_hash text NOT NULL,
 chunk_set_id uuid NOT NULL,index_id uuid NOT NULL,chunk_count integer NOT NULL,token_total bigint NOT NULL,
 embedding_digest text NOT NULL,ledger_digest text NOT NULL,coverage text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,job_id,attempt_id,fence),UNIQUE(org_id,job_id),UNIQUE(org_id,index_id),
 FOREIGN KEY(org_id,job_id,attempt_id,fence) REFERENCES public.embed_attempt_specs(org_id,job_id,id,fence),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.embed_specs(org_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_set_id) REFERENCES public.retrieval_chunk_sets(org_id,repository_id,snapshot_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,index_id,chunk_set_id) REFERENCES public.search_indices(org_id,repository_id,snapshot_id,id,chunk_set_id),
 CHECK(fence>0 AND spec_hash ~ '^[0-9a-f]{64}$' AND embedding_digest ~ '^[0-9a-f]{64}$' AND ledger_digest ~ '^[0-9a-f]{64}$'),
 CHECK(chunk_count BETWEEN 1 AND 4096 AND token_total>=0 AND coverage IN ('complete','partial'))
);
CREATE TABLE public.embed_vector_stage (
 org_id uuid NOT NULL,spec_id uuid NOT NULL,chunk_id uuid NOT NULL,chunk_set_id uuid NOT NULL,
 repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,reservation_id uuid NOT NULL,
 embedding_json jsonb NOT NULL,token_count integer NOT NULL,provider_request_id text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,spec_id,chunk_id),
 FOREIGN KEY(org_id,spec_id) REFERENCES public.embed_specs(org_id,id),
 FOREIGN KEY(org_id,repository_id,snapshot_id,chunk_id,chunk_set_id) REFERENCES public.source_chunks(org_id,repository_id,snapshot_id,id,chunk_set_id),
 CHECK(jsonb_typeof(embedding_json)='array' AND jsonb_array_length(embedding_json) IN (256,512,1024)
   AND octet_length(embedding_json::text)<=262144),
 CHECK(token_count>=0 AND (provider_request_id IS NULL OR octet_length(provider_request_id) BETWEEN 1 AND 255))
);
CREATE INDEX ix_embed_stage_set ON public.embed_vector_stage(org_id,chunk_set_id,spec_id);
CREATE TRIGGER guard_embed_specs_immutable BEFORE UPDATE OR DELETE ON public.embed_specs
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
CREATE TRIGGER guard_embed_attempts_immutable BEFORE UPDATE OR DELETE ON public.embed_attempt_specs
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
CREATE TRIGGER guard_embed_receipts_immutable BEFORE UPDATE OR DELETE ON public.embed_publication_receipts
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
-- Staged vectors may be removed (after publication), never rewritten.
CREATE FUNCTION public.codespy_embed_stage_no_update() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN RAISE EXCEPTION 'staged embedding is immutable' USING ERRCODE='23514'; END $$;
CREATE TRIGGER guard_embed_stage_no_update BEFORE UPDATE ON public.embed_vector_stage
 FOR EACH ROW EXECUTE FUNCTION public.codespy_embed_stage_no_update();
REVOKE ALL ON FUNCTION public.codespy_embed_stage_no_update() FROM PUBLIC;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.embed_specs) OR EXISTS(SELECT 1 FROM public.embed_vector_stage)
  OR EXISTS(SELECT 1 FROM public.embed_publication_receipts)
  OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='embed_chunk_set')
  OR EXISTS(SELECT 1 FROM public.search_indices WHERE chunk_set_id IS NOT NULL AND index_type='dense')
 THEN RAISE EXCEPTION 'dense index history exists' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.embed_vector_stage;
DROP TABLE public.embed_publication_receipts;
DROP TABLE public.embed_attempt_specs;
DROP FUNCTION public.codespy_embed_stage_no_update();
DROP INDEX public.uq_index_set_dense;
DROP INDEX public.uq_index_set_lexical;
CREATE UNIQUE INDEX uq_index_set_kind ON public.search_indices(org_id,chunk_set_id,index_type) WHERE chunk_set_id IS NOT NULL;
ALTER TABLE public.search_indices DROP CONSTRAINT ck_index_qualified;
ALTER TABLE public.search_indices ADD CONSTRAINT ck_index_qualified CHECK(chunk_set_id IS NULL OR
 (index_type='lexical' AND state IN ('ready','partial_ready') AND sealed_at IS NOT NULL
  AND embedding_provider IS NULL AND embedding_model IS NULL AND dimension IS NULL
  AND id=public.uuid_generate_v5(chunk_set_id,'lexical-v1')));
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_embed;
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_index;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_index CHECK(
 (operation='index_snapshot' AND index_spec_id IS NOT NULL AND snapshot_id IS NOT NULL
  AND submission_id IS NULL AND parent_job_id IS NULL AND analysis_spec_id IS NULL
  AND scan_spec_id IS NULL AND result_run_id IS NULL) OR
 (operation<>'index_snapshot' AND index_spec_id IS NULL AND result_index_id IS NULL));
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN
 ('admit_source','inventory_snapshot','analyze_structure','scan_snapshot','index_snapshot','investigate_snapshot'));
ALTER TABLE public.jobs DROP CONSTRAINT fk_job_embed_spec;
ALTER TABLE public.jobs DROP COLUMN embed_spec_id;
DROP TABLE public.embed_specs;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
