"""Additive scan storage; historical job projections remain independent."""

from typing import Any

import sqlalchemy as sa

from app.models.phase2_tables import ck, col, created, fk, md, orgid, pk, uq
from app.models.phase3_tables import structural_jobs


def canonical(name: str, payload: str, digest: str, maximum: int) -> list[Any]:
    return [
        col(payload, "json"),
        col("canonical_text"),
        col(digest),
        ck(
            f"ck_{name}_json",
            f"canonical_text IS JSON OBJECT WITH UNIQUE KEYS "
            f"AND canonical_text::jsonb={payload} AND octet_length(canonical_text)<={maximum}",
        ),
        ck(
            f"ck_{name}_digest", f"{digest}=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')"
        ),
    ]


def scope() -> list[sa.Column[Any]]:
    return [orgid(), col("snapshot_id", "uuid"), col("run_id", "uuid")]


def report_fk(name: str) -> sa.ForeignKeyConstraint:
    return fk(
        f"fk_{name}_report",
        ["org_id", "snapshot_id", "run_id"],
        "scan_reports",
        ["org_id", "snapshot_id", "run_id"],
    )


def file_fk(name: str) -> sa.ForeignKeyConstraint:
    return fk(
        f"fk_{name}_file",
        ["org_id", "snapshot_id", "file_id"],
        "source_files",
        ["org_id", "snapshot_id", "id"],
    )


scan_specs = sa.Table(
    "scan_specs",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("principal_id", "uuid"),
    *canonical("scan_specs", "config_json", "config_hash", 65536),
    created(),
    pk("org_id", "id"),
    uq("uq_scan_spec_scope", "org_id", "repository_id", "snapshot_id", "id", "config_hash"),
    fk(
        "fk_scan_spec_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    fk(
        "fk_scan_spec_member", ["org_id", "principal_id"], "memberships", ["org_id", "principal_id"]
    ),
)
scan_attempt_specs = sa.Table(
    "scan_attempt_specs",
    md,
    *scope(),
    col("repository_id", "uuid"),
    col("spec_id", "uuid"),
    col("config_hash"),
    col("job_id", "uuid"),
    col("attempt_id", "uuid"),
    col("fence", "bigint"),
    *canonical("scan_attempt_specs", "publication_spec_json", "spec_digest", 98304),
    col("captured_at", "time"),
    created(),
    pk("org_id", "snapshot_id", "run_id"),
    uq("uq_scan_attempt_job", "org_id", "job_id", "attempt_id"),
    uq("uq_scan_attempt_spec", "org_id", "snapshot_id", "run_id", "spec_id", "config_hash"),
    fk(
        "fk_scan_attempt_spec",
        ["org_id", "repository_id", "snapshot_id", "spec_id", "config_hash"],
        "scan_specs",
        ["org_id", "repository_id", "snapshot_id", "id", "config_hash"],
    ),
    fk(
        "fk_scan_attempt_run",
        ["org_id", "snapshot_id", "run_id"],
        "analysis_runs",
        ["org_id", "snapshot_id", "id"],
    ),
    fk(
        "fk_scan_attempt_worker",
        ["org_id", "job_id", "attempt_id", "fence"],
        "job_attempts",
        ["org_id", "job_id", "id", "fence"],
    ),
    ck("ck_scan_attempt_fence", "fence>0"),
)
scan_reports = sa.Table(
    "scan_reports",
    md,
    *scope(),
    col("repository_id", "uuid"),
    col("spec_id", "uuid"),
    col("config_hash"),
    *canonical("scan_reports", "draft_json", "draft_digest", 4194304),
    col("batch_hash"),
    col("coverage"),
    col("omitted_findings", "bigint"),
    created(),
    pk("org_id", "snapshot_id", "run_id"),
    fk(
        "fk_scan_report_attempt",
        ["org_id", "snapshot_id", "run_id", "spec_id", "config_hash"],
        "scan_attempt_specs",
        ["org_id", "snapshot_id", "run_id", "spec_id", "config_hash"],
    ),
    ck("ck_scan_report_coverage", "coverage IN ('complete','partial','unknown')"),
    ck("ck_scan_report_omissions", "omitted_findings BETWEEN 0 AND 502000"),
    ck("ck_scan_report_batch", "batch_hash ~ '^[0-9a-f]{64}$'"),
)
scan_observations = sa.Table(
    "scan_observations",
    md,
    *scope(),
    col("observation_key"),
    col("origin"),
    col("kind"),
    col("file_id", "uuid"),
    *canonical("scan_observations", "payload_json", "payload_digest", 262144),
    created(),
    pk("org_id", "snapshot_id", "run_id", "observation_key"),
    report_fk("scan_observation"),
    file_fk("scan_observation"),
    ck(
        "ck_scan_observation_key",
        "observation_key=origin||':'||payload_digest AND octet_length(observation_key)<=80",
    ),
    ck(
        "ck_scan_observation_kind",
        "(origin IN ('semgrep','gitleaks') AND kind='native') OR "
        "(origin='dependencies' AND kind IN ('dependency_instance','unresolved_dependency'))",
    ),
)
finding_occurrences = sa.Table(
    "finding_occurrences",
    md,
    *scope(),
    col("id", "uuid"),
    col("file_id", "uuid"),
    col("observation_key"),
    col("category"),
    col("rule_id"),
    *canonical("finding_occurrences", "payload_json", "payload_digest", 32768),
    created(),
    pk("org_id", "snapshot_id", "run_id", "id"),
    report_fk("finding"),
    file_fk("finding"),
    fk(
        "fk_finding_observation",
        ["org_id", "snapshot_id", "run_id", "observation_key"],
        "scan_observations",
        ["org_id", "snapshot_id", "run_id", "observation_key"],
    ),
    ck(
        "ck_finding_category",
        "category IN ('source_pattern','secret_format','dependency_advisory')",
    ),
    ck("ck_finding_rule", "rule_id ~ '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$'"),
)
finding_support = sa.Table(
    "finding_support",
    md,
    *scope(),
    col("finding_id", "uuid"),
    col("evidence_id", "uuid"),
    pk("org_id", "snapshot_id", "run_id", "finding_id", "evidence_id"),
    fk(
        "fk_finding_support_occurrence",
        ["org_id", "snapshot_id", "run_id", "finding_id"],
        "finding_occurrences",
        ["org_id", "snapshot_id", "run_id", "id"],
    ),
    fk(
        "fk_finding_support_evidence",
        ["org_id", "snapshot_id", "run_id", "evidence_id"],
        "evidence_records",
        ["org_id", "snapshot_id", "run_id", "id"],
    ),
)
verification_decisions = sa.Table(
    "verification_decisions",
    md,
    *scope(),
    col("finding_id", "uuid"),
    col("disposition"),
    *canonical("verification_decisions", "payload_json", "payload_digest", 8192),
    created(),
    pk("org_id", "snapshot_id", "run_id", "finding_id"),
    fk(
        "fk_scan_decision_finding",
        ["org_id", "snapshot_id", "run_id", "finding_id"],
        "finding_occurrences",
        ["org_id", "snapshot_id", "run_id", "id"],
    ),
    ck(
        "ck_scan_decision_disposition",
        "disposition IN ('proposed','supported-static','contradicted','unresolved')",
    ),
)
SCAN_TABLES = {
    t.name: t
    for t in (
        scan_specs,
        scan_attempt_specs,
        scan_reports,
        scan_observations,
        finding_occurrences,
        finding_support,
        verification_decisions,
    )
}
for name, table, columns in (
    (
        "ix_scan_specs_history",
        scan_specs,
        ("org_id", "repository_id", "snapshot_id", "created_at", "id"),
    ),
    (
        "ix_scan_reports_history",
        scan_reports,
        ("org_id", "repository_id", "snapshot_id", "created_at", "run_id"),
    ),
    ("ix_findings_file", finding_occurrences, ("org_id", "snapshot_id", "run_id", "file_id", "id")),
    (
        "ix_finding_support_reverse",
        finding_support,
        ("org_id", "snapshot_id", "run_id", "evidence_id", "finding_id"),
    ),
    (
        "ix_scan_observations_file",
        scan_observations,
        ("org_id", "snapshot_id", "run_id", "file_id", "observation_key"),
    ),
):
    sa.Index(name, *[table.c[c] for c in columns])

scan_jobs = sa.Table(
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
        for c in structural_jobs.columns
    ],
    col("scan_spec_id", "uuid", True),
)
