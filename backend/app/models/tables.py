import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base

organizations = sa.Table(
    "organizations",
    Base.metadata,
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

principals = sa.Table(
    "principals",
    Base.metadata,
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
    sa.CheckConstraint("octet_length(subject_key) BETWEEN 1 AND 255", name="ck_principal_subject"),
    sa.CheckConstraint("length(display_name) BETWEEN 1 AND 200", name="ck_principal_name"),
)

memberships = sa.Table(
    "memberships",
    Base.metadata,
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
sa.Index("ix_member_principal", memberships.c.principal_id, memberships.c.org_id)

repositories = sa.Table(
    "repositories",
    Base.metadata,
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
    sa.CheckConstraint("source_kind IN ('local_fixture','github','archive')", name="ck_repo_kind"),
)

artifacts = sa.Table(
    "artifacts",
    Base.metadata,
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
sa.Index(
    "ix_artifact_history",
    artifacts.c.org_id,
    artifacts.c.repository_id,
    artifacts.c.created_at,
    artifacts.c.id,
)

source_snapshots = sa.Table(
    "source_snapshots",
    Base.metadata,
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
    sa.UniqueConstraint("org_id", "repository_id", "identity_hash", name="uq_snapshot_identity"),
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
sa.Index(
    "ix_snapshot_history",
    source_snapshots.c.org_id,
    source_snapshots.c.repository_id,
    source_snapshots.c.created_at,
    source_snapshots.c.id,
)

source_files = sa.Table(
    "source_files",
    Base.metadata,
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

analysis_runs = sa.Table(
    "analysis_runs",
    Base.metadata,
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
sa.Index(
    "ix_run_history",
    analysis_runs.c.org_id,
    analysis_runs.c.snapshot_id,
    analysis_runs.c.created_at,
    analysis_runs.c.id,
)

cpg_nodes = sa.Table(
    "cpg_nodes",
    Base.metadata,
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
        "node_kind IN ('module','class','function','method','call','import')", name="ck_node_kind"
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
sa.Index(
    "ix_node_outline",
    cpg_nodes.c.org_id,
    cpg_nodes.c.snapshot_id,
    cpg_nodes.c.run_id,
    cpg_nodes.c.file_id,
    cpg_nodes.c.start_byte,
    cpg_nodes.c.local_id,
)

evidence_records = sa.Table(
    "evidence_records",
    Base.metadata,
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
sa.Index(
    "ix_evidence_file",
    evidence_records.c.org_id,
    evidence_records.c.snapshot_id,
    evidence_records.c.run_id,
    evidence_records.c.file_id,
)

cpg_edges = sa.Table(
    "cpg_edges",
    Base.metadata,
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
sa.Index(
    "ix_edge_forward",
    cpg_edges.c.org_id,
    cpg_edges.c.snapshot_id,
    cpg_edges.c.run_id,
    cpg_edges.c.source_local_id,
    cpg_edges.c.edge_kind,
    postgresql_include=["target_local_id", "support_evidence_id"],
)
sa.Index(
    "ix_edge_reverse",
    cpg_edges.c.org_id,
    cpg_edges.c.snapshot_id,
    cpg_edges.c.run_id,
    cpg_edges.c.target_local_id,
    cpg_edges.c.edge_kind,
    postgresql_include=["source_local_id", "support_evidence_id"],
)
sa.Index(
    "ix_edge_support",
    cpg_edges.c.org_id,
    cpg_edges.c.snapshot_id,
    cpg_edges.c.run_id,
    cpg_edges.c.support_evidence_id,
)

evidence_derivations = sa.Table(
    "evidence_derivations",
    Base.metadata,
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
sa.Index(
    "ix_derivation_parent",
    evidence_derivations.c.org_id,
    evidence_derivations.c.snapshot_id,
    evidence_derivations.c.run_id,
    evidence_derivations.c.parent_id,
    evidence_derivations.c.child_id,
)


# Explicit table mappings; persistence uses scalar projections, without lazy relationships.
class Organizations(Base):
    __table__ = organizations


class Principals(Base):
    __table__ = principals


class Memberships(Base):
    __table__ = memberships


class Repositories(Base):
    __table__ = repositories


class Artifacts(Base):
    __table__ = artifacts


class SourceSnapshots(Base):
    __table__ = source_snapshots


class SourceFiles(Base):
    __table__ = source_files


class AnalysisRuns(Base):
    __table__ = analysis_runs


class CpgNodes(Base):
    __table__ = cpg_nodes


class EvidenceRecords(Base):
    __table__ = evidence_records


class CpgEdges(Base):
    __table__ = cpg_edges


class EvidenceDerivations(Base):
    __table__ = evidence_derivations
