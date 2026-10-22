"""Evidence-backed repository memory registry."""

from alembic import op

revision = "0019_repo_memory"
down_revision = "0018_search_indices"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.repo_memories (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    memory_kind VARCHAR(32) NOT NULL,
    topic VARCHAR(128) NOT NULL,
    summary TEXT NOT NULL,
    evidence_pointers JSONB DEFAULT '[]'::jsonb NOT NULL,
    tsv TSVECTOR DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT fk_mem_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_mem_snapshot FOREIGN KEY (org_id, snapshot_id)
        REFERENCES public.source_snapshots (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_repo_memories_topic ON public.repo_memories (
    org_id, repository_id, topic
);

CREATE INDEX ix_repo_memories_tsv ON public.repo_memories USING GIN (tsv);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.repo_memories TO codespy_app;
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DROP TABLE IF EXISTS public.repo_memories CASCADE;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
