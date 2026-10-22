"""Source code chunks registry and full-text search indexes."""

from alembic import op

revision = "0017_search_chunks"
down_revision = "0016_derived_artifacts"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.source_chunks (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    file_id UUID NOT NULL,
    file_path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    chunk_kind VARCHAR(32) NOT NULL,
    symbol_name TEXT DEFAULT NULL,
    qualified_name TEXT DEFAULT NULL,
    cpg_node_id UUID DEFAULT NULL,
    start_byte BIGINT NOT NULL,
    end_byte BIGINT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content_sha256 VARCHAR(64) NOT NULL,
    byte_count INTEGER NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    safe_content TEXT NOT NULL,
    redaction_digest VARCHAR(64) NOT NULL,
    chunker_version VARCHAR(32) NOT NULL,
    tsv TSVECTOR DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT uq_chunk_scope UNIQUE (
        org_id, snapshot_id, file_id, chunk_index, chunker_version
    ),
    CONSTRAINT ck_chunk_byte_bounds CHECK (start_byte >= 0 AND end_byte >= start_byte),
    CONSTRAINT ck_chunk_line_bounds CHECK (start_line >= 1 AND end_line >= start_line),
    CONSTRAINT ck_chunk_counts CHECK (byte_count >= 0 AND estimated_tokens >= 0),
    CONSTRAINT fk_chunk_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_chunk_source_file FOREIGN KEY (org_id, snapshot_id, file_id)
        REFERENCES public.source_files (org_id, snapshot_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_source_chunks_scope ON public.source_chunks (
    org_id, snapshot_id, file_id
);

CREATE INDEX ix_source_chunks_path ON public.source_chunks (
    org_id, snapshot_id, file_path
);

CREATE INDEX ix_source_chunks_symbol ON public.source_chunks (
    org_id, snapshot_id, symbol_name
);

CREATE INDEX ix_source_chunks_tsv ON public.source_chunks USING GIN (tsv);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.source_chunks TO codespy_app;
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DROP TABLE IF EXISTS public.source_chunks CASCADE;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
