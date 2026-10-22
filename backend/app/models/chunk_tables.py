"""SQLAlchemy table definition for source code chunks and full-text search indexes."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR

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
    created(),
    pk("org_id", "id"),
    uq(
        "uq_chunk_scope",
        "org_id",
        "snapshot_id",
        "file_id",
        "chunk_index",
        "chunker_version",
    ),
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
