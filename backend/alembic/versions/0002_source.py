"""Initial source schema; historical declarations are independent of current models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_source"
down_revision = "0001_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", UUID(as_uuid=True), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_sha256_hash"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "id"),
        sa.UniqueConstraint("org_id", "repository_id", "id", name="uq_artifact_repo"),
        sa.ForeignKeyConstraint(
            ["org_id", "repository_id"],
            ["repositories.org_id", "repositories.id"],
            name="fk_artifact_repo",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint("size_bytes>=0", name="ck_artifact_size"),
        sa.CheckConstraint("octet_length(storage_key) BETWEEN 1 AND 1024", name="ck_artifact_key"),
    )
    op.create_index(
        "ix_artifact_history", "artifacts", ["org_id", "repository_id", "created_at", "id"]
    )
    op.create_table(
        "source_snapshots",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("repository_id", UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_revision", sa.Text(), nullable=True),
        sa.Column("admission_config_hash", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "admission_config_hash ~ '^[0-9a-f]{64}$'", name="ck_admission_config_hash_hash"
        ),
        sa.Column("identity_hash", sa.Text(), nullable=False),
        sa.CheckConstraint("identity_hash ~ '^[0-9a-f]{64}$'", name="ck_identity_hash_hash"),
        sa.Column("manifest_hash", sa.Text(), nullable=True),
        sa.CheckConstraint("manifest_hash ~ '^[0-9a-f]{64}$'", name="ck_manifest_hash_hash"),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'building'")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("org_id", "id"),
        sa.UniqueConstraint("org_id", "repository_id", "id", name="uq_snapshot_repo"),
        sa.UniqueConstraint(
            "org_id", "repository_id", "identity_hash", name="uq_snapshot_identity"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "repository_id"],
            ["repositories.org_id", "repositories.id"],
            name="fk_snapshot_repo",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "repository_id", "artifact_id"],
            ["artifacts.org_id", "artifacts.repository_id", "artifacts.id"],
            name="fk_snapshot_artifact",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint(
            "(state='building' AND manifest_hash IS NULL AND sealed_at IS NULL) OR (state='sealed' AND manifest_hash IS NOT NULL AND sealed_at IS NOT NULL)",  # noqa: E501
            name="ck_snapshot_state",
        ),
        sa.CheckConstraint(
            "source_revision IS NULL OR octet_length(source_revision) BETWEEN 1 AND 255",
            name="ck_snapshot_revision",
        ),
    )
    op.create_index(
        "ix_snapshot_history", "source_snapshots", ["org_id", "repository_id", "created_at", "id"]
    )
    op.create_table(
        "source_files",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text(collation="C"), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_sha256_hash"),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "id"),
        sa.UniqueConstraint("org_id", "snapshot_id", "path", name="uq_file_path"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id"],
            ["source_snapshots.org_id", "source_snapshots.id"],
            name="fk_file_snapshot",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint("size_bytes>=0", name="ck_file_size"),
        sa.CheckConstraint(
            "octet_length(path) BETWEEN 1 AND 1024 AND path !~ '(^/|//|/$|(^|/)[.]{1,2}(/|$))' AND position(chr(92) in path)=0",  # noqa: E501
            name="ck_file_path",
        ),
    )


def downgrade() -> None:
    op.drop_table("source_files")
    op.drop_table("source_snapshots")
    op.drop_table("artifacts")
