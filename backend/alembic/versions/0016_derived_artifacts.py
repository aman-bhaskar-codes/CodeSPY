"""Derived artifacts registry for model-safe source views and privacy transformations."""

from alembic import op

revision = "0016_derived_artifacts"
down_revision = "0015_provider_budget"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.derived_artifacts (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    file_id UUID NOT NULL,
    representation VARCHAR(32) NOT NULL,
    policy_version VARCHAR(64) NOT NULL,
    source_sha256 VARCHAR(64) NOT NULL,
    derived_sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    redaction_digest VARCHAR(64) NOT NULL,
    transformation_status VARCHAR(32) NOT NULL,
    interval_map_json JSONB NOT NULL,
    storage_key TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT uq_derived_artifact_scope UNIQUE (
        org_id, snapshot_id, file_id, representation, policy_version
    ),
    CONSTRAINT ck_derived_size_nonneg CHECK (size_bytes >= 0),
    CONSTRAINT ck_derived_status CHECK (
        transformation_status IN ('ready', 'failed', 'tombstoned')
    ),
    CONSTRAINT ck_derived_representation CHECK (
        representation IN ('model_safe', 'redacted_ast')
    ),
    CONSTRAINT fk_derived_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_derived_source_file FOREIGN KEY (org_id, snapshot_id, file_id)
        REFERENCES public.source_files (org_id, snapshot_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_derived_artifacts_lookup ON public.derived_artifacts (
    org_id, snapshot_id, file_id, representation, policy_version
);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.derived_artifacts TO codespy_app;
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DROP TABLE IF EXISTS public.derived_artifacts CASCADE;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
