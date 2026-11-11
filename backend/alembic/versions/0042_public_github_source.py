"""Allow submissions acquired from public GitHub archives."""

from alembic import op

revision = "0042_public_github_source"
down_revision = "0041_native_vector_embeddings"
branch_labels = None
depends_on = None

UP = r"""
ALTER TABLE public.source_submissions DROP CONSTRAINT ck_submission_kind;
ALTER TABLE public.source_submissions ADD CONSTRAINT ck_submission_kind
 CHECK (source_kind IN ('upload_zip','github_archive','github_public_archive'));
"""

DOWN = r"""
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.source_submissions WHERE source_kind='github_public_archive') THEN
  RAISE EXCEPTION 'public GitHub submissions exist' USING ERRCODE='23514';
 END IF;
END $$;
ALTER TABLE public.source_submissions DROP CONSTRAINT ck_submission_kind;
ALTER TABLE public.source_submissions ADD CONSTRAINT ck_submission_kind
 CHECK (source_kind IN ('upload_zip','github_archive'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
