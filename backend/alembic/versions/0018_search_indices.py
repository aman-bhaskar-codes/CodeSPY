"""Search indices and chunk embeddings lifecycle registry."""

from alembic import op

revision = "0018_search_indices"
down_revision = "0017_search_chunks"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.search_indices (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    index_type VARCHAR(32) NOT NULL,
    state VARCHAR(32) NOT NULL,
    embedding_provider VARCHAR(64) DEFAULT NULL,
    embedding_model VARCHAR(128) DEFAULT NULL,
    dimension INTEGER DEFAULT NULL,
    total_chunks INTEGER DEFAULT 0 NOT NULL,
    indexed_chunks INTEGER DEFAULT 0 NOT NULL,
    failed_chunks INTEGER DEFAULT 0 NOT NULL,
    sealed_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT ck_index_state CHECK (
        state IN ('building', 'sealing', 'ready', 'partial_ready', 'failed', 'stale')
    ),
    CONSTRAINT ck_index_type CHECK (
        index_type IN ('lexical', 'dense', 'hybrid')
    ),
    CONSTRAINT fk_index_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_index_snapshot FOREIGN KEY (org_id, snapshot_id)
        REFERENCES public.source_snapshots (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_search_indices_scope ON public.search_indices (
    org_id, snapshot_id, index_type, state
);

CREATE INDEX ix_search_indices_repo ON public.search_indices (
    org_id, repository_id
);

CREATE TABLE public.chunk_embeddings (
    org_id UUID NOT NULL,
    chunk_id UUID NOT NULL,
    index_id UUID NOT NULL,
    repository_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    embedding_json JSONB NOT NULL,
    token_count INTEGER DEFAULT 0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, chunk_id, index_id),
    CONSTRAINT fk_emb_chunk FOREIGN KEY (org_id, chunk_id)
        REFERENCES public.source_chunks (org_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_emb_index FOREIGN KEY (org_id, index_id)
        REFERENCES public.search_indices (org_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_emb_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_emb_snapshot FOREIGN KEY (org_id, snapshot_id)
        REFERENCES public.source_snapshots (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_chunk_embeddings_index ON public.chunk_embeddings (
    org_id, index_id
);

CREATE INDEX ix_chunk_embeddings_snapshot ON public.chunk_embeddings (
    org_id, snapshot_id
);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.search_indices TO codespy_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.chunk_embeddings TO codespy_app;
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DROP TABLE IF EXISTS public.chunk_embeddings CASCADE;
DROP TABLE IF EXISTS public.search_indices CASCADE;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
