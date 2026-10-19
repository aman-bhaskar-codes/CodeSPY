"""Additive structural metadata; historical migrations remain independent."""

import sqlalchemy as sa

from app.models.phase2_tables import ck, col, created, fk, jobs, md, orgid, pk, uq

analysis_specs = sa.Table(
    "analysis_specs",
    md,
    orgid(),
    col("id", "uuid"),
    col("repository_id", "uuid"),
    col("snapshot_id", "uuid"),
    col("principal_id", "uuid"),
    col("config_json", "json"),
    col("config_hash"),
    col("toolchain_hash"),
    created(),
    pk("org_id", "id"),
    uq("uq_spec_scope", "org_id", "repository_id", "snapshot_id", "id", "config_hash"),
    fk(
        "fk_spec_snapshot",
        ["org_id", "repository_id", "snapshot_id"],
        "source_snapshots",
        ["org_id", "repository_id", "id"],
    ),
    fk("fk_spec_member", ["org_id", "principal_id"], "memberships", ["org_id", "principal_id"]),
    ck(
        "ck_spec_config",
        "jsonb_typeof(config_json)='object' AND octet_length(config_json::text)<=98304",
    ),
    ck("ck_spec_hashes", "config_hash ~ '^[0-9a-f]{64}$' AND toolchain_hash ~ '^[0-9a-f]{64}$'"),
)
structural_reports = sa.Table(
    "structural_reports",
    md,
    orgid(),
    col("snapshot_id", "uuid"),
    col("run_id", "uuid"),
    col("spec_id", "uuid"),
    col("repository_id", "uuid"),
    col("config_hash"),
    col("report_json", "json"),
    col("report_sha256"),
    col("normalized_sha256"),
    created(),
    pk("org_id", "snapshot_id", "run_id"),
    fk(
        "fk_report_run",
        ["org_id", "snapshot_id", "run_id"],
        "analysis_runs",
        ["org_id", "snapshot_id", "id"],
    ),
    fk(
        "fk_report_spec",
        ["org_id", "repository_id", "snapshot_id", "spec_id", "config_hash"],
        "analysis_specs",
        ["org_id", "repository_id", "snapshot_id", "id", "config_hash"],
    ),
    ck(
        "ck_report_json",
        "jsonb_typeof(report_json)='object' AND octet_length(report_json::text)<=4194304",
    ),
    ck(
        "ck_report_hashes",
        "config_hash ~ '^[0-9a-f]{64}$' AND report_sha256 ~ '^[0-9a-f]{64}$' "
        "AND normalized_sha256 ~ '^[0-9a-f]{64}$'",
    ),
)
for table, tail in [(analysis_specs, "id"), (structural_reports, "run_id")]:
    sa.Index(
        "ix_" + table.name + "_history",
        *[table.c[n] for n in ["org_id", "repository_id", "snapshot_id", "created_at", tail]],
    )
jobs.append_column(col("analysis_spec_id", "uuid", True))
for constraint in list(jobs.constraints):
    if constraint.name == "ck_job_operation":
        jobs.constraints.remove(constraint)
jobs.append_constraint(
    ck("ck_job_operation", "operation IN ('admit_source','inventory_snapshot','analyze_structure')")
)
jobs.append_constraint(
    ck(
        "ck_job_structure",
        "(operation='analyze_structure' AND analysis_spec_id IS NOT NULL "
        "AND snapshot_id IS NOT NULL AND submission_id IS NULL) "
        "OR (operation<>'analyze_structure' AND analysis_spec_id IS NULL)",
    )
)
jobs.append_constraint(
    fk(
        "fk_job_spec",
        ["org_id", "repository_id", "snapshot_id", "analysis_spec_id", "config_hash"],
        "analysis_specs",
        ["org_id", "repository_id", "snapshot_id", "id", "config_hash"],
    )
)
