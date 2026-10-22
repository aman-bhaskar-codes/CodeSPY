"""SQLAlchemy table definition for derived artifacts and safe source views."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

derived_artifacts = sa.Table(
    "derived_artifacts",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("file_id", "uuid"),
    sa.Column("representation", sa.String(32), nullable=False),
    sa.Column("policy_version", sa.String(64), nullable=False),
    sa.Column("source_sha256", sa.String(64), nullable=False),
    sa.Column("derived_sha256", sa.String(64), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("redaction_digest", sa.String(64), nullable=False),
    sa.Column("transformation_status", sa.String(32), nullable=False),
    sa.Column(
        "interval_map_json",
        sa.JSON().with_variant(JSONB, "postgresql"),
        nullable=False,
    ),
    sa.Column("storage_key", sa.Text, nullable=True),
    created(),
    sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
    pk("org_id", "id"),
    uq(
        "uq_derived_artifact_scope",
        "org_id",
        "snapshot_id",
        "file_id",
        "representation",
        "policy_version",
    ),
    ck("ck_derived_size_nonneg", "size_bytes >= 0"),
    ck(
        "ck_derived_status",
        "transformation_status IN ('ready', 'failed', 'tombstoned')",
    ),
    ck(
        "ck_derived_representation",
        "representation IN ('model_safe', 'redacted_ast')",
    ),
    fk(
        "fk_derived_repo",
        ["org_id", "repository_id"],
        "repositories",
        ["org_id", "id"],
    ),
    fk(
        "fk_derived_source_file",
        ["org_id", "snapshot_id", "file_id"],
        "source_files",
        ["org_id", "snapshot_id", "id"],
    ),
)
