"""SQLAlchemy models for durable investigation execution.

Defines schemas for investigation specs, execution checkpoints, step receipts,
retained model call outcomes, evidence records, reports, publication receipts,
and command idempotency.
"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from app.models.index_tables import index_jobs
from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq


def _scope() -> list[sa.Column[Any]]:
    return [orgid(), col("repository_id", "uuid"), col("snapshot_id", "uuid")]


# ---------------------------------------------------------------------------
# Investigation Specs (immutable canonical specifications)
# ---------------------------------------------------------------------------

investigation_specs = sa.Table(
    "investigation_specs",
    md,
    orgid(),
    col("id", "uuid"),
    col("spec_hash"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("principal_id", "uuid"),
    sa.Column("target_type", sa.String(32), nullable=False),
    col("finding_id", "uuid", nullable=True),
    col("finding_run_id", "uuid", nullable=True),
    col("chunk_set_id", "uuid", nullable=True),
    col("chunk_config_id", "uuid", nullable=True),
    col("materialization_id", "uuid", nullable=True),
    sa.Column("query", sa.Text, nullable=False),
    sa.Column("mode", sa.String(32), nullable=False),
    sa.Column("depth", sa.String(32), nullable=False),
    sa.Column("limits_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    col("disclosure_policy_digest"),
    sa.Column("tool_policy_revision", sa.String(128), nullable=False),
    sa.Column("model_route_revision", sa.String(128), nullable=False),
    sa.Column("model_profile_revision", sa.String(128), nullable=False),
    sa.Column("runtime_revision", sa.Integer, server_default="1", nullable=False),
    sa.Column("privacy_policy_revision", sa.String(128), nullable=False),
    created(),
    pk("org_id", "id"),
    uq("uq_investigation_spec_hash", "org_id", "spec_hash"),
    fk(
        "fk_investigation_spec_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    fk(
        "fk_investigation_spec_member",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
    ck("ck_investigation_spec_hash_format", "length(spec_hash) = 64"),
    ck("ck_investigation_spec_disclosure_format", "length(disclosure_policy_digest) = 64"),
    ck("ck_investigation_spec_target_type", "target_type IN ('repository', 'finding')"),
    ck("ck_investigation_spec_mode", "mode IN ('short_path', 'two_specialist')"),
    ck("ck_investigation_spec_depth", "depth IN ('quick', 'standard', 'deep')"),
    ck("ck_investigation_spec_runtime_revision", "runtime_revision >= 1"),
)


# ---------------------------------------------------------------------------
# Investigation Execution Checkpoints (versioned, job-backed checkpoints)
# ---------------------------------------------------------------------------

investigation_execution_checkpoints = sa.Table(
    "investigation_execution_checkpoints",
    md,
    orgid(),
    col("investigation_id", "uuid"),
    sa.Column("execution_revision", sa.Integer, server_default="1", nullable=False),
    sa.Column("checkpoint_sequence", sa.BigInteger, nullable=False),
    col("spec_hash"),
    col("node_id", "uuid"),
    sa.Column("node_kind", sa.String(32), nullable=False),
    sa.Column("role", sa.String(32), nullable=False),
    sa.Column("logical_ordinal", sa.Integer, nullable=False),
    sa.Column("state_snapshot_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    col("state_digest"),
    col("writer_job_id", "uuid"),
    col("writer_attempt_id", "uuid"),
    sa.Column("writer_fencing_token", sa.BigInteger, nullable=False),
    created(),
    pk("org_id", "investigation_id", "execution_revision", "checkpoint_sequence"),
    fk(
        "fk_exec_checkpoint_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    ck("ck_exec_checkpoint_seq_nonneg", "checkpoint_sequence >= 0"),
    ck("ck_exec_checkpoint_rev_pos", "execution_revision >= 1"),
    ck("ck_exec_checkpoint_fence_pos", "writer_fencing_token >= 1"),
    ck("ck_exec_checkpoint_spec_hash", "length(spec_hash) = 64"),
    ck("ck_exec_checkpoint_digest", "length(state_digest) = 64"),
)


# ---------------------------------------------------------------------------
# Investigation Step Receipts (per-node completion / error receipts)
# ---------------------------------------------------------------------------

investigation_step_receipts = sa.Table(
    "investigation_step_receipts",
    md,
    orgid(),
    col("investigation_id", "uuid"),
    sa.Column("execution_revision", sa.Integer, server_default="1", nullable=False),
    col("node_id", "uuid"),
    sa.Column("node_kind", sa.String(32), nullable=False),
    sa.Column("role", sa.String(32), nullable=False),
    sa.Column("logical_ordinal", sa.Integer, nullable=False),
    col("input_digest"),
    sa.Column("state", sa.String(32), nullable=False),
    col("result_digest", nullable=True),
    sa.Column("call_ref", sa.String(128), nullable=True),
    col("reservation_id", "uuid", nullable=True),
    col("model_result_id", "uuid", nullable=True),
    col("attempt_id", "uuid"),
    created(),
    pk("org_id", "investigation_id", "execution_revision", "node_id"),
    fk(
        "fk_step_receipt_investigation",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    ck("ck_step_receipt_state", "state IN ('started', 'completed', 'failed', 'uncertain')"),
    ck("ck_step_receipt_rev_pos", "execution_revision >= 1"),
    ck("ck_step_receipt_input_digest", "length(input_digest) = 64"),
)


# ---------------------------------------------------------------------------
# Model Call Results (encrypted full outcome store for crash-safe replay)
# ---------------------------------------------------------------------------

model_call_results = sa.Table(
    "model_call_results",
    md,
    orgid(),
    col("id", "uuid"),
    col("reservation_id", "uuid"),
    col("logical_call_id", "uuid"),
    col("request_digest"),
    sa.Column("model_id", sa.String(128), nullable=False),
    sa.Column("provider", sa.String(64), nullable=False),
    sa.Column("profile_revision", sa.String(128), nullable=False),
    sa.Column("outcome_status", sa.String(32), nullable=False),
    sa.Column("ciphertext", sa.LargeBinary, nullable=True),
    col("ciphertext_digest", nullable=True),
    sa.Column("failure_code", sa.String(64), nullable=True),
    created(),
    col("expires_at", "time"),
    pk("org_id", "id"),
    uq("uq_model_call_result_reservation", "org_id", "reservation_id"),
    ck("ck_model_call_result_status", "outcome_status IN ('usable', 'invalid', 'unusable')"),
    ck("ck_model_call_request_digest", "length(request_digest) = 64"),
)


# ---------------------------------------------------------------------------
# Investigation Evidence (immutable source / tool observations)
# ---------------------------------------------------------------------------

investigation_evidence = sa.Table(
    "investigation_evidence",
    md,
    orgid(),
    col("id", "uuid"),
    col("investigation_id", "uuid"),
    col("snapshot_id", "uuid"),
    sa.Column("source_path", sa.Text, nullable=False),
    col("file_sha256"),
    sa.Column("original_start_line", sa.Integer, nullable=False),
    sa.Column("original_end_line", sa.Integer, nullable=False),
    sa.Column("safe_start_line", sa.Integer, nullable=False),
    sa.Column("safe_end_line", sa.Integer, nullable=False),
    col("excerpt_sha256"),
    sa.Column("observation_type", sa.String(64), nullable=False),
    sa.Column("coverage_summary", sa.Text, nullable=True),
    sa.Column("limitations", sa.Text, nullable=True),
    col("tool_receipt_id", "uuid", nullable=True),
    created(),
    pk("org_id", "id"),
    fk(
        "fk_investigation_evidence_inv",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    ck("ck_evidence_file_sha256", "length(file_sha256) = 64"),
    ck("ck_evidence_excerpt_sha256", "length(excerpt_sha256) = 64"),
    ck(
        "ck_evidence_orig_lines",
        "original_start_line >= 1 AND original_start_line <= original_end_line",
    ),
    ck("ck_evidence_safe_lines", "safe_start_line >= 1 AND safe_start_line <= safe_end_line"),
)


# ---------------------------------------------------------------------------
# Investigation Reports (immutable final publication)
# ---------------------------------------------------------------------------

investigation_reports = sa.Table(
    "investigation_reports",
    md,
    orgid(),
    col("id", "uuid"),
    col("investigation_id", "uuid"),
    col("spec_hash"),
    sa.Column("execution_revision", sa.Integer, server_default="1", nullable=False),
    col("job_id", "uuid"),
    sa.Column("verdict", sa.String(32), nullable=False),
    sa.Column("verdict_confidence", sa.Float, nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("report_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    col("report_digest"),
    sa.Column("claims_count", sa.Integer, nullable=False),
    sa.Column("evidence_refs_count", sa.Integer, nullable=False),
    created(),
    pk("org_id", "id"),
    uq("uq_investigation_report_job", "org_id", "job_id"),
    fk(
        "fk_investigation_report_inv",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    ck("ck_investigation_report_spec_hash", "length(spec_hash) = 64"),
    ck("ck_investigation_report_digest", "length(report_digest) = 64"),
    ck("ck_investigation_report_claims_nonneg", "claims_count >= 0"),
    ck("ck_investigation_report_evidence_nonneg", "evidence_refs_count >= 0"),
)


# ---------------------------------------------------------------------------
# Investigation Publication Receipts (atomic finalization binding)
# ---------------------------------------------------------------------------

investigation_publication_receipts = sa.Table(
    "investigation_publication_receipts",
    md,
    orgid(),
    col("id", "uuid"),
    col("investigation_id", "uuid"),
    col("spec_hash"),
    col("job_id", "uuid"),
    col("attempt_id", "uuid"),
    sa.Column("fencing_token", sa.BigInteger, nullable=False),
    col("report_id", "uuid"),
    col("report_digest"),
    col("draft_digest"),
    sa.Column(
        "published_at",
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    pk("org_id", "id"),
    uq("uq_investigation_publication_job", "org_id", "job_id"),
    fk(
        "fk_publication_receipt_inv",
        ["org_id", "investigation_id"],
        "investigations",
        ["org_id", "id"],
    ),
    fk(
        "fk_publication_receipt_report",
        ["org_id", "report_id"],
        "investigation_reports",
        ["org_id", "id"],
    ),
    ck("ck_publication_spec_hash", "length(spec_hash) = 64"),
    ck("ck_publication_fence_pos", "fencing_token >= 1"),
    ck("ck_publication_report_digest", "length(report_digest) = 64"),
    ck("ck_publication_draft_digest", "length(draft_digest) = 64"),
)


# ---------------------------------------------------------------------------
# Investigation Commands (durable idempotency receipts)
# ---------------------------------------------------------------------------

investigation_commands = sa.Table(
    "investigation_commands",
    md,
    orgid(),
    col("id", "uuid"),
    sa.Column("command_kind", sa.String(32), nullable=False),
    sa.Column("idempotency_key", sa.String(128), nullable=False),
    col("request_digest"),
    col("principal_id", "uuid"),
    col("investigation_id", "uuid"),
    col("job_id", "uuid", nullable=True),
    sa.Column("status_code", sa.Integer, nullable=False),
    sa.Column("response_json", JSONB().with_variant(sa.JSON(), "sqlite"), nullable=False),
    created(),
    pk("org_id", "id"),
    uq("uq_investigation_command_key", "org_id", "command_kind", "idempotency_key"),
    fk(
        "fk_investigation_command_member",
        ["org_id", "principal_id"],
        "memberships",
        ["org_id", "principal_id"],
    ),
    ck(
        "ck_command_kind",
        "command_kind IN ('create', 'start', 'cancel', 'resume', 'retry')",
    ),
    ck("ck_command_request_digest", "length(request_digest) = 64"),
)


# ---------------------------------------------------------------------------
# Investigation Jobs Projection
# ---------------------------------------------------------------------------

investigation_jobs = sa.Table(
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
        for c in index_jobs.columns
    ],
    col("investigation_id", "uuid", True),
    col("spec_hash", "text", True),
    col("result_report_id", "uuid", True),
)
