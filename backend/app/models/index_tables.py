"""Scoped lexical materializations and independent index job projection."""

from typing import Any

import sqlalchemy as sa

from app.models.chunk_tables import chunk_embeddings, search_indices, source_chunks
from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq
from app.models.scan_tables import scan_jobs


def _scope() -> list[sa.Column[Any]]:
    return [orgid(), col("repository_id", "uuid"), col("snapshot_id", "uuid")]


index_specs = sa.Table(
    "index_specs",
    md,
    *_scope(),
    col("id", "uuid"),
    col("principal_id", "uuid"),
    col("config_hash"),
    col("spec_hash"),
    col("config_json", "json"),
    col("spec_json", "json"),
    col("canonical_text"),
    col("source_object_id", "uuid"),
    col("manifest_object_id", "uuid"),
    created(),
    pk("org_id", "id"),
    uq(
        "uq_index_spec_binding",
        "org_id",
        "repository_id",
        "snapshot_id",
        "id",
        "config_hash",
        "principal_id",
    ),
    fk(
        "fk_index_spec_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    fk(
        "fk_index_spec_principal",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
    *[
        fk(
            "fk_index_spec_" + field,
            ["org_id", "repository_id", field],
            "object_records",
            ["org_id", "repository_id", "id"],
        )
        for field in ("source_object_id", "manifest_object_id")
    ],
    ck("ck_index_spec_hashes", "config_hash ~ '^[0-9a-f]{64}$' AND spec_hash ~ '^[0-9a-f]{64}$'"),
)
retrieval_chunk_sets = sa.Table(
    "retrieval_chunk_sets",
    md,
    *_scope(),
    col("id", "uuid"),
    col("config_hash"),
    col("materialization_hash"),
    col("manifest_hash"),
    col("manifest_json", "json"),
    col("canonical_text"),
    col("chunk_count", "int"),
    col("safe_bytes", "bigint"),
    col("coverage"),
    created(),
    pk("org_id", "id"),
    uq("uq_materialization_config", "org_id", "repository_id", "snapshot_id", "config_hash"),
    uq("uq_materialization_scope", "org_id", "repository_id", "snapshot_id", "id"),
    fk(
        "fk_materialization_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    ck(
        "ck_materialization_counts",
        "chunk_count BETWEEN 0 AND 4096 AND safe_bytes BETWEEN 0 AND 16777216",
    ),
    ck("ck_materialization_coverage", "coverage IN ('complete','partial')"),
)
index_attempt_specs = sa.Table(
    "index_attempt_specs",
    md,
    *_scope(),
    col("id", "uuid"),
    col("job_id", "uuid"),
    col("fence", "bigint"),
    col("spec_id", "uuid"),
    col("spec_hash"),
    created(),
    pk("org_id", "id"),
    uq("uq_index_attempt", "org_id", "job_id", "id", "fence"),
    fk(
        "fk_index_attempt_job",
        ["org_id", "job_id", "id", "fence"],
        "job_attempts",
        ["org_id", "job_id", "id", "fence"],
    ),
    fk("fk_index_attempt_spec", ["org_id", "spec_id"], "index_specs", ["org_id", "id"]),
)
index_publication_receipts = sa.Table(
    "index_publication_receipts",
    md,
    *_scope(),
    col("job_id", "uuid"),
    col("attempt_id", "uuid"),
    col("fence", "bigint"),
    col("spec_id", "uuid"),
    col("spec_hash"),
    col("chunk_set_id", "uuid"),
    col("index_id", "uuid"),
    col("draft_digest"),
    col("materialization_hash"),
    col("coverage"),
    created(),
    pk("org_id", "job_id", "attempt_id", "fence"),
    uq("uq_index_receipt_job", "org_id", "job_id"),
    fk(
        "fk_index_receipt_attempt",
        ["org_id", "job_id", "attempt_id", "fence"],
        "index_attempt_specs",
        ["org_id", "job_id", "id", "fence"],
    ),
    fk("fk_index_receipt_spec", ["org_id", "spec_id"], "index_specs", ["org_id", "id"]),
    fk(
        "fk_index_receipt_set",
        ["org_id", "repository_id", "snapshot_id", "chunk_set_id"],
        "retrieval_chunk_sets",
        ["org_id", "repository_id", "snapshot_id", "id"],
    ),
    fk(
        "fk_index_receipt_index",
        ["org_id", "repository_id", "snapshot_id", "index_id", "chunk_set_id"],
        "search_indices",
        ["org_id", "repository_id", "snapshot_id", "id", "chunk_set_id"],
    ),
)
for table in (source_chunks, search_indices):
    table.append_constraint(
        fk(
            "fk_" + table.name + "_qualified_set",
            ["org_id", "repository_id", "snapshot_id", "chunk_set_id"],
            "retrieval_chunk_sets",
            ["org_id", "repository_id", "snapshot_id", "id"],
        )
    )
for field, target in (("chunk_id", "source_chunks"), ("index_id", "search_indices")):
    chunk_embeddings.append_constraint(
        fk(
            "fk_embedding_" + field + "_set",
            ["org_id", "repository_id", "snapshot_id", field, "chunk_set_id"],
            target,
            ["org_id", "repository_id", "snapshot_id", "id", "chunk_set_id"],
        )
    )
sa.Index(
    "uq_index_set_kind",
    search_indices.c.org_id,
    search_indices.c.chunk_set_id,
    search_indices.c.index_type,
    unique=True,
    postgresql_where=search_indices.c.chunk_set_id.is_not(None),
    sqlite_where=search_indices.c.chunk_set_id.is_not(None),
)
index_jobs = sa.Table(
    "jobs",
    sa.MetaData(),
    *[
        sa.Column(
            c.name,
            c.type,
            nullable=c.nullable,
            primary_key=c.primary_key,
            server_default=c.server_default,
        )
        for c in scan_jobs.columns
    ],
    col("index_spec_id", "uuid", True),
    col("result_index_id", "uuid", True),
)
