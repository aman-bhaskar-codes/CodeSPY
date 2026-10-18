"""Initial identity schema; historical declarations are independent of current models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0001_identity"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_org_slug"),
        sa.CheckConstraint("slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'", name="ck_org_slug"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="ck_org_name"),
    )
    op.create_table(
        "principals",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("subject_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("subject_key", name="uq_principal_subject"),
        sa.CheckConstraint(
            "octet_length(subject_key) BETWEEN 1 AND 255", name="ck_principal_subject"
        ),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="ck_principal_name"),
    )
    op.create_table(
        "memberships",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("principal_id", UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "principal_id"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_member_org",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_member_principal",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint("role IN ('owner','admin','member','viewer')", name="ck_member_role"),
    )
    op.create_index("ix_member_principal", "memberships", ["principal_id", "org_id"])
    op.create_table(
        "repositories",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("repository_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "id"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_repo_org",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.UniqueConstraint("org_id", "repository_key", name="uq_repo_key"),
        sa.CheckConstraint("octet_length(repository_key) BETWEEN 1 AND 255", name="ck_repo_key"),
        sa.CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="ck_repo_name"),
        sa.CheckConstraint(
            "source_kind IN ('local_fixture','github','archive')", name="ck_repo_kind"
        ),
    )


def downgrade() -> None:
    op.drop_table("repositories")
    op.drop_table("memberships")
    op.drop_table("principals")
    op.drop_table("organizations")
