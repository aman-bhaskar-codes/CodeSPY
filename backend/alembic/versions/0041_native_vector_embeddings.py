"""Add native pgvector storage and an HNSW cosine index for chunk embeddings."""

from alembic import op

revision = "0041_native_vector_embeddings"
down_revision = "0040_investigation_runtime_permissions"
branch_labels = None
depends_on = None

# The dimension-less column holds any supported profile (Titan v2 offers 256/512/1024).
# HNSW needs a fixed dimension, so the 1024-dimension profile gets a partial expression
# index; other dimensions fall back to an exact scan within the scoped rows.
UPGRADE = r"""
ALTER TABLE public.chunk_embeddings ADD COLUMN embedding vector;

CREATE FUNCTION public.codespy_embedding_vector_sync() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public AS $$
BEGIN
 IF NEW.embedding IS NULL AND jsonb_typeof(NEW.embedding_json)='array' THEN
  NEW.embedding := (NEW.embedding_json::text)::vector;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION public.codespy_embedding_vector_sync FROM PUBLIC;

CREATE TRIGGER codespy_embedding_vector_sync BEFORE INSERT ON public.chunk_embeddings
FOR EACH ROW EXECUTE FUNCTION public.codespy_embedding_vector_sync();

UPDATE public.chunk_embeddings
SET embedding=(embedding_json::text)::vector
WHERE embedding IS NULL AND jsonb_typeof(embedding_json)='array';

CREATE INDEX ix_chunk_embeddings_hnsw_1024 ON public.chunk_embeddings
USING hnsw ((embedding::vector(1024)) vector_cosine_ops)
WHERE vector_dims(embedding)=1024;
"""

DOWNGRADE = r"""
DROP INDEX public.ix_chunk_embeddings_hnsw_1024;
DROP TRIGGER codespy_embedding_vector_sync ON public.chunk_embeddings;
DROP FUNCTION public.codespy_embedding_vector_sync();
ALTER TABLE public.chunk_embeddings DROP COLUMN embedding;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    # embedding_json remains the canonical copy, so the vector column is fully derivable.
    op.execute(DOWNGRADE)
