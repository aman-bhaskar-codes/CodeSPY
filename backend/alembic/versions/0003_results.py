"""Initial results schema; historical declarations are independent of current models."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003_results"
down_revision = "0002_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analysis_runs",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("request_key", sa.Text(), nullable=False),
        sa.Column("analyzer_name", sa.Text(), nullable=False),
        sa.Column("analyzer_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=False),
        sa.CheckConstraint("config_hash ~ '^[0-9a-f]{64}$'", name="ck_config_hash_hash"),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(config)='object'", name="ck_config_object"),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("write_version", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("coverage", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(coverage)='object'", name="ck_coverage_object"),
        sa.Column("limitations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(limitations)='array'", name="ck_limitations_array"),
        sa.Column("result_hash", sa.Text(), nullable=True),
        sa.CheckConstraint("result_hash ~ '^[0-9a-f]{64}$'", name="ck_result_hash_hash"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "id"),
        sa.UniqueConstraint("org_id", "request_key", name="uq_run_request"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id"],
            ["source_snapshots.org_id", "source_snapshots.id"],
            name="fk_run_snapshot",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint(
            "octet_length(request_key) BETWEEN 1 AND 128 AND octet_length(analyzer_name) BETWEEN 1 AND 128 AND octet_length(analyzer_version) BETWEEN 1 AND 128",  # noqa: E501
            name="ck_run_text",
        ),
        sa.CheckConstraint("write_version>=0", name="ck_run_version"),
        sa.CheckConstraint(
            "status IN ('queued','running','completed','partial','failed','cancelled')",
            name="ck_run_status",
        ),
        sa.CheckConstraint(
            "(status IN ('completed','partial','failed','cancelled'))=(finished_at IS NOT NULL) AND (status NOT IN ('running','completed','partial') OR started_at IS NOT NULL)",  # noqa: E501
            name="ck_run_times",
        ),
        sa.CheckConstraint(
            "(status IN ('completed','partial'))=(result_hash IS NOT NULL)", name="ck_run_result"
        ),
        sa.CheckConstraint(
            "(status='failed' AND failure_code IS NOT NULL AND octet_length(failure_code) BETWEEN 1 AND 128) OR (status<>'failed' AND failure_code IS NULL)",  # noqa: E501
            name="ck_run_failure",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('completed','partial') OR (coverage ? 'status' AND ((status='completed' AND coverage->>'status'='complete') OR (status='partial' AND coverage->>'status' IN ('partial','unknown','unavailable'))))) IS TRUE",  # noqa: E501
            name="ck_run_coverage",
        ),
    )
    op.create_index(
        "ix_run_history", "analysis_runs", ["org_id", "snapshot_id", "created_at", "id"]
    )
    op.create_table(
        "cpg_nodes",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("local_id", UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", UUID(as_uuid=True), nullable=False),
        sa.Column("node_kind", sa.Text(), nullable=False),
        sa.Column("symbol_name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("start_byte", sa.BigInteger(), nullable=False),
        sa.Column("end_byte", sa.BigInteger(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("start_col", sa.Integer(), nullable=False),
        sa.Column("end_col", sa.Integer(), nullable=False),
        sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(attributes)='object'", name="ck_attributes_object"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "run_id", "local_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id"],
            ["analysis_runs.org_id", "analysis_runs.snapshot_id", "analysis_runs.id"],
            name="fk_node_run",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "file_id"],
            ["source_files.org_id", "source_files.snapshot_id", "source_files.id"],
            name="fk_node_file",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint(
            "node_kind IN ('module','class','function','method','call','import')",
            name="ck_node_kind",
        ),
        sa.CheckConstraint(
            "octet_length(symbol_name)<=2048 AND octet_length(qualified_name)<=2048",
            name="ck_node_names",
        ),
        sa.CheckConstraint(
            "start_byte>=0 AND end_byte>=start_byte AND start_line>=1 AND end_line>=start_line AND start_col>=0 AND end_col>=0 AND (end_line>start_line OR end_col>=start_col)",  # noqa: E501
            name="ck_node_range",
        ),
    )
    op.create_index(
        "ix_node_outline",
        "cpg_nodes",
        ["org_id", "snapshot_id", "run_id", "file_id", "start_byte", "local_id"],
    )
    op.create_table(
        "evidence_records",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", UUID(as_uuid=True), nullable=True),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_digest_hash"),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("provider_name", sa.Text(), nullable=False),
        sa.Column("provider_version", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("observation_mode", sa.Text(), nullable=False),
        sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("coverage_status", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(payload)='object'", name="ck_payload_object"),
        sa.Column("limitations", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(limitations)='array'", name="ck_limitations_array"),
        sa.Column("start_byte", sa.BigInteger(), nullable=True),
        sa.Column("end_byte", sa.BigInteger(), nullable=True),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("start_col", sa.Integer(), nullable=True),
        sa.Column("end_col", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "run_id", "id"),
        sa.UniqueConstraint("org_id", "snapshot_id", "run_id", "digest", name="uq_evidence_digest"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id"],
            ["analysis_runs.org_id", "analysis_runs.snapshot_id", "analysis_runs.id"],
            name="fk_evidence_run",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "file_id"],
            ["source_files.org_id", "source_files.snapshot_id", "source_files.id"],
            name="fk_evidence_file",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint("schema_version=1", name="ck_evidence_schema"),
        sa.CheckConstraint(
            "octet_length(provider_name) BETWEEN 1 AND 128 AND octet_length(provider_version) BETWEEN 1 AND 128 AND octet_length(operation) BETWEEN 1 AND 128",  # noqa: E501
            name="ck_evidence_provider",
        ),
        sa.CheckConstraint(
            "observation_mode IN ('static','runtime','external') AND basis IN ('observed','derived','inferred') AND coverage_status IN ('complete','partial','unknown','unavailable')",  # noqa: E501
            name="ck_evidence_mode",
        ),
        sa.CheckConstraint(
            "num_nonnulls(start_byte,end_byte,start_line,end_line,start_col,end_col)=0 OR (file_id IS NOT NULL AND num_nonnulls(start_byte,end_byte,start_line,end_line,start_col,end_col)=6 AND start_byte>=0 AND end_byte>=start_byte AND start_line>=1 AND end_line>=start_line AND start_col>=0 AND end_col>=0 AND (end_line>start_line OR end_col>=start_col))",  # noqa: E501
            name="ck_evidence_range",
        ),
    )
    op.create_index(
        "ix_evidence_file", "evidence_records", ["org_id", "snapshot_id", "run_id", "file_id"]
    )
    op.create_table(
        "cpg_edges",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_local_id", UUID(as_uuid=True), nullable=False),
        sa.Column("target_local_id", UUID(as_uuid=True), nullable=False),
        sa.Column("edge_kind", sa.Text(), nullable=False),
        sa.Column("support_evidence_id", UUID(as_uuid=True), nullable=False),
        sa.Column("properties", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint("jsonb_typeof(properties)='object'", name="ck_properties_object"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "run_id", "id"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id", "source_local_id"],
            ["cpg_nodes.org_id", "cpg_nodes.snapshot_id", "cpg_nodes.run_id", "cpg_nodes.local_id"],
            name="fk_edge_source",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id", "target_local_id"],
            ["cpg_nodes.org_id", "cpg_nodes.snapshot_id", "cpg_nodes.run_id", "cpg_nodes.local_id"],
            name="fk_edge_target",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id", "support_evidence_id"],
            [
                "evidence_records.org_id",
                "evidence_records.snapshot_id",
                "evidence_records.run_id",
                "evidence_records.id",
            ],
            name="fk_edge_support",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.UniqueConstraint(
            "org_id",
            "snapshot_id",
            "run_id",
            "source_local_id",
            "target_local_id",
            "edge_kind",
            "support_evidence_id",
            name="uq_edge_relation",
        ),
        sa.CheckConstraint(
            "edge_kind IN ('AST_CHILD','IMPORTS','CALLS_CANDIDATE')", name="ck_edge_kind"
        ),
    )
    op.create_index(
        "ix_edge_forward",
        "cpg_edges",
        ["org_id", "snapshot_id", "run_id", "source_local_id", "edge_kind"],
        postgresql_include=["target_local_id", "support_evidence_id"],
    )
    op.create_index(
        "ix_edge_reverse",
        "cpg_edges",
        ["org_id", "snapshot_id", "run_id", "target_local_id", "edge_kind"],
        postgresql_include=["source_local_id", "support_evidence_id"],
    )
    op.create_index(
        "ix_edge_support", "cpg_edges", ["org_id", "snapshot_id", "run_id", "support_evidence_id"]
    )
    op.create_table(
        "evidence_derivations",
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("child_id", UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("org_id", "snapshot_id", "run_id", "child_id", "parent_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id", "child_id"],
            [
                "evidence_records.org_id",
                "evidence_records.snapshot_id",
                "evidence_records.run_id",
                "evidence_records.id",
            ],
            name="fk_derivation_child",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "snapshot_id", "run_id", "parent_id"],
            [
                "evidence_records.org_id",
                "evidence_records.snapshot_id",
                "evidence_records.run_id",
                "evidence_records.id",
            ],
            name="fk_derivation_parent",
            ondelete="RESTRICT",
            onupdate="NO ACTION",
        ),
        sa.CheckConstraint("child_id<>parent_id", name="ck_derivation_self"),
    )
    op.create_index(
        "ix_derivation_parent",
        "evidence_derivations",
        ["org_id", "snapshot_id", "run_id", "parent_id", "child_id"],
    )


def downgrade() -> None:
    op.drop_table("evidence_derivations")
    op.drop_table("cpg_edges")
    op.drop_table("evidence_records")
    op.drop_table("cpg_nodes")
    op.drop_table("analysis_runs")
