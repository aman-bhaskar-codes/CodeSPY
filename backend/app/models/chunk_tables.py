"""SQLAlchemy table definition for source code chunks and full-text search indexes."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq

source_chunks = sa.Table(
    "source_chunks",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("file_id", "uuid"),
    sa.Column("file_path", sa.Text, nullable=False),
    sa.Column("chunk_index", sa.Integer, nullable=False),
    sa.Column("chunk_kind", sa.String(32), nullable=False),
    sa.Column("symbol_name", sa.Text, nullable=True),
    sa.Column("qualified_name", sa.Text, nullable=True),
    col("cpg_node_id", "uuid", nullable=True),
    sa.Column("start_byte", sa.BigInteger, nullable=False),
    sa.Column("end_byte", sa.BigInteger, nullable=False),
    sa.Column("start_line", sa.Integer, nullable=False),
    sa.Column("end_line", sa.Integer, nullable=False),
    sa.Column("content_sha256", sa.String(64), nullable=False),
    sa.Column("byte_count", sa.Integer, nullable=False),
    sa.Column("estimated_tokens", sa.Integer, nullable=False),
    sa.Column("safe_content", sa.Text, nullable=False),
    sa.Column("redaction_digest", sa.String(64), nullable=False),
    sa.Column("chunker_version", sa.String(32), nullable=False),
    sa.Column("tsv", TSVECTOR().with_variant(sa.Text, "sqlite"), nullable=True),
    col("chunk_set_id", "uuid", nullable=True),
    sa.Column("safe_content_sha256", sa.Text, nullable=True),
    sa.Column("safe_start_byte", sa.BigInteger, nullable=True),
    sa.Column("safe_end_byte", sa.BigInteger, nullable=True),
    sa.Column("provenance_json", JSONB().with_variant(sa.JSON, "sqlite"), nullable=True),
    created(),
    pk("org_id", "id"),
    uq("uq_chunk_set_scope", "org_id", "repository_id", "snapshot_id", "id", "chunk_set_id"),
    ck("ck_chunk_byte_bounds", "start_byte >= 0 AND end_byte >= start_byte"),
    ck("ck_chunk_line_bounds", "start_line >= 1 AND end_line >= start_line"),
    ck("ck_chunk_counts", "byte_count >= 0 AND estimated_tokens >= 0"),
    fk("fk_chunk_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_chunk_source_file",
        ["org_id", "snapshot_id", "file_id"],
        "source_files",
        ["org_id", "snapshot_id", "id"],
    ),
)

sa.Index(
    "uq_chunk_legacy",
    *[
        source_chunks.c[n]
        for n in ("org_id", "snapshot_id", "file_id", "chunk_index", "chunker_version")
    ],
    unique=True,
    postgresql_where=source_chunks.c.chunk_set_id.is_(None),
    sqlite_where=source_chunks.c.chunk_set_id.is_(None),
)
sa.Index(
    "uq_chunk_materialized",
    source_chunks.c.org_id,
    source_chunks.c.chunk_set_id,
    source_chunks.c.file_id,
    source_chunks.c.chunk_index,
    unique=True,
    postgresql_where=source_chunks.c.chunk_set_id.is_not(None),
    sqlite_where=source_chunks.c.chunk_set_id.is_not(None),
)


search_indices = sa.Table(
    "search_indices",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    sa.Column("index_type", sa.String(32), nullable=False),
    sa.Column("state", sa.String(32), nullable=False),
    sa.Column("embedding_provider", sa.String(64), nullable=True),
    sa.Column("embedding_model", sa.String(128), nullable=True),
    sa.Column("dimension", sa.Integer, nullable=True),
    sa.Column("total_chunks", sa.Integer, nullable=False, server_default="0"),
    sa.Column("indexed_chunks", sa.Integer, nullable=False, server_default="0"),
    sa.Column("failed_chunks", sa.Integer, nullable=False, server_default="0"),
    sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column(
        "metadata_json",
        JSONB().with_variant(sa.JSON, "sqlite"),
        nullable=False,
        server_default="{}",
    ),
    col("chunk_set_id", "uuid", nullable=True),
    created(),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.clock_timestamp(),
    ),
    pk("org_id", "id"),
    uq("uq_index_result_scope", "org_id", "repository_id", "snapshot_id", "id"),
    uq("uq_index_set_scope", "org_id", "repository_id", "snapshot_id", "id", "chunk_set_id"),
    ck(
        "ck_index_state",
        "state IN ('building', 'sealing', 'ready', 'partial_ready', 'failed', 'stale')",
    ),
    ck("ck_index_type", "index_type IN ('lexical', 'dense', 'hybrid')"),
    fk("fk_index_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_index_snapshot",
        ["org_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "id"],
    ),
)

chunk_embeddings = sa.Table(
    "chunk_embeddings",
    md,
    orgid(),
    col("chunk_id", "uuid"),
    col("index_id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    sa.Column(
        "embedding_json",
        JSONB().with_variant(sa.JSON, "sqlite"),
        nullable=False,
    ),
    sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
    created(),
    col("chunk_set_id", "uuid", nullable=True),
    pk("org_id", "chunk_id", "index_id"),
    sa.ForeignKeyConstraint(
        ["org_id", "chunk_id"],
        ["source_chunks.org_id", "source_chunks.id"],
        name="fk_emb_chunk",
        ondelete="CASCADE",
    ),
    sa.ForeignKeyConstraint(
        ["org_id", "index_id"],
        ["search_indices.org_id", "search_indices.id"],
        name="fk_emb_index",
        ondelete="CASCADE",
    ),
    fk("fk_emb_repo", ["org_id", "repository_id"], "repositories", ["org_id", "id"]),
    fk(
        "fk_emb_snapshot",
        ["org_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "id"],
    ),
)
