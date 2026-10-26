"""ingestion metadata. Historical DDL is frozen independently in Alembic revisions."""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base
from app.models import tables as legacy

md = Base.metadata
spec: list[tuple[str, int, sa.Table]] = []


def col(
    n: str, t: str = "text", nullable: bool = False, default: str | None = None
) -> sa.Column[Any]:
    types: dict[str, Any] = {
        "text": sa.Text,
        "uuid": lambda: UUID(as_uuid=True),
        "bigint": sa.BigInteger,
        "int": sa.Integer,
        "bool": sa.Boolean,
        "time": lambda: sa.DateTime(timezone=True),
        "json": JSONB,
        "bytes": sa.LargeBinary,
    }
    return sa.Column(
        n,
        types[t](),
        nullable=nullable,
        server_default=sa.text(default) if default is not None else None,
    )


def created() -> sa.Column[Any]:
    return col("created_at", "time", default="CURRENT_TIMESTAMP")


def pk(*a: str) -> sa.PrimaryKeyConstraint:
    return sa.PrimaryKeyConstraint(*a)


def uq(n: str, *a: str) -> sa.UniqueConstraint:
    return sa.UniqueConstraint(*a, name=n)


def fk(
    n: str, a: Sequence[str], t: str, b: Sequence[str], defer: bool = False
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        a,
        [t + "." + x for x in b],
        name=n,
        ondelete="RESTRICT",
        deferrable=True if defer else None,
        initially="DEFERRED" if defer else None,
    )


def ck(n: str, e: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(e, name=n)


def orgid() -> sa.Column[Any]:
    return col("org_id", "uuid")


def ident() -> sa.Column[Any]:
    return col("id", "uuid")


def obj(n: str) -> list[Any]:
    return [
        col(n, "json", default="'{}'::jsonb"),
        ck("ck_" + n + "_object", f"jsonb_typeof({n})='object'"),
    ]


def table(
    name: str, r: int, items: Sequence[Any], indexes: Sequence[tuple[str, Sequence[str]]] = ()
) -> sa.Table:
    t = sa.Table(name, md, *items)
    for n, fields in indexes:
        sa.Index(n, *[t.c[f] for f in fields])
    spec.append((name, r, t))
    return t


def orgfk(n: str) -> sa.ForeignKeyConstraint:
    return fk(n, ["org_id"], "organizations", ["id"])


def repofk(n: str) -> sa.ForeignKeyConstraint:
    return fk(n, ["org_id", "repository_id"], "repositories", ["org_id", "id"])


def memberfk(n: str) -> sa.ForeignKeyConstraint:
    return fk(n, ["org_id", "principal_id"], "memberships", ["org_id", "principal_id"])


table(
    "external_identities",
    5,
    [
        col("provider"),
        col("subject"),
        col("principal_id", "uuid"),
        created(),
        pk("provider", "subject"),
        uq("uq_identity_principal", "provider", "principal_id"),
        fk("fk_external_principal", ["principal_id"], "principals", ["id"]),
    ],
)
table(
    "oauth_flows",
    5,
    [
        ident(),
        col("state_hash"),
        col("browser_hash"),
        col("verifier_cipher", "bytes"),
        col("key_id"),
        col("return_path"),
        col("expires_at", "time"),
        col("consumed_at", "time", True),
        created(),
        pk("id"),
        uq("uq_oauth_state", "state_hash"),
    ],
    [("ix_oauth_expiry", ["expires_at"])],
)
table(
    "sessions",
    5,
    [
        ident(),
        col("principal_id", "uuid"),
        col("token_hash"),
        col("csrf_hash"),
        col("issued_at", "time"),
        col("last_seen_at", "time"),
        col("idle_expires_at", "time"),
        col("absolute_expires_at", "time"),
        col("revoked_at", "time", True),
        pk("id"),
        uq("uq_session_token", "token_hash"),
        fk("fk_session_principal", ["principal_id"], "principals", ["id"]),
    ],
    [
        ("ix_session_principal", ["principal_id", "revoked_at"]),
        ("ix_session_expiry", ["absolute_expires_at"]),
    ],
)
table(
    "github_user_grants",
    5,
    [
        col("principal_id", "uuid"),
        col("token_cipher", "bytes"),
        col("key_id"),
        col("expires_at", "time"),
        col("revoked_at", "time", True),
        col("version", "bigint", default="0"),
        col("last_verified_at", "time"),
        pk("principal_id"),
        fk("fk_github_principal", ["principal_id"], "principals", ["id"]),
        ck("ck_github_grant_version", "version>=0 AND length(key_id)>0"),
    ],
)
table(
    "repository_grants",
    5,
    [
        orgid(),
        col("repository_id", "uuid"),
        col("principal_id", "uuid"),
        col("permission"),
        col("revoked_at", "time", True),
        created(),
        pk("org_id", "repository_id", "principal_id"),
        repofk("fk_grant_repository"),
        memberfk("fk_grant_membership"),
        ck("ck_repo_permission", "permission IN ('read','write')"),
    ],
    [("ix_grant_principal", ["org_id", "principal_id", "repository_id"])],
)
table(
    "github_repository_bindings",
    5,
    [
        orgid(),
        col("repository_id", "uuid"),
        col("github_repository_id", "bigint"),
        col("installation_id", "bigint"),
        col("account_id", "bigint"),
        col("state"),
        col("version", "bigint", default="0"),
        col("last_checked_at", "time"),
        pk("org_id", "repository_id"),
        uq("uq_binding_github", "org_id", "github_repository_id"),
        repofk("fk_binding_repo"),
        ck(
            "ck_binding_ids",
            "github_repository_id>0 AND installation_id>0 AND account_id>0 AND version>=0",
        ),
        ck("ck_binding_state", "state IN ('active','suspended','revoked')"),
    ],
    [("ix_binding_installation", ["installation_id"])],
)
table(
    "org_work_limits",
    5,
    [
        orgid(),
        col("max_active_jobs", "int", default="2"),
        col("max_pending_jobs", "int", default="20"),
        col("max_reserved_bytes", "bigint", default="1073741824"),
        col("reserved_bytes", "bigint", default="0"),
        created(),
        pk("org_id"),
        orgfk("fk_limits_org"),
        ck(
            "ck_work_limits",
            "max_active_jobs>0 AND max_pending_jobs>0 AND reserved_bytes>=0 AND "
            "reserved_bytes<=max_reserved_bytes",
        ),
    ],
)
objects = table(
    "object_records",
    6,
    [
        orgid(),
        ident(),
        col("repository_id", "uuid"),
        col("key"),
        col("kind"),
        col("state"),
        col("sha256", nullable=True),
        col("size_bytes", "bigint", True),
        col("version_id", nullable=True),
        col("owner_job_id", "uuid", True),
        col("owner_fence", "bigint", True),
        col("expires_at", "time", True),
        created(),
        pk("org_id", "id"),
        uq("uq_object_scope", "org_id", "repository_id", "id"),
        uq("uq_object_key", "org_id", "repository_id", "key"),
        repofk("fk_object_repo"),
        ck("ck_object_kind", "kind IN ('input','stage','source','manifest')"),
        ck(
            "ck_object_state",
            "state IN "
            "('reserved','writing','quarantined','verified','available','deleting','deleted')",
        ),
        ck(
            "ck_object_digest",
            "state NOT IN ('verified','available') OR (sha256 ~ '^[0-9a-f]{64}$' AND "
            "sha256 IS NOT NULL AND size_bytes>=0 AND size_bytes IS NOT NULL)",
        ),
        ck("ck_object_owner", "(owner_job_id IS NULL)=(owner_fence IS NULL)"),
    ],
    [("ix_object_expiry", ["state", "expires_at"])],
)
submissions = table(
    "source_submissions",
    6,
    [
        orgid(),
        ident(),
        col("repository_id", "uuid"),
        col("principal_id", "uuid"),
        col("source_kind"),
        col("state"),
        col("request_key"),
        col("request_hash"),
        col("retry_of_submission_id", "uuid", True),
        col("reserved_bytes", "bigint"),
        col("reservation_released_at", "time", True),
        col("finished_at", "time", True),
        col("declared_size", "bigint", True),
        col("declared_sha", nullable=True),
        col("actual_size", "bigint", True),
        col("actual_sha", nullable=True),
        col("input_object_id", "uuid", True),
        col("snapshot_id", "uuid", True),
        col("requested_ref", nullable=True),
        col("resolved_commit", nullable=True),
        col("policy_hash"),
        col("error_code", nullable=True),
        col("upload_owner", nullable=True),
        col("upload_expires_at", "time", True),
        col("expires_at", "time"),
        created(),
        pk("org_id", "id"),
        uq("uq_submission_scope", "org_id", "repository_id", "id"),
        uq("uq_submission_key", "org_id", "request_key"),
        repofk("fk_submission_repo"),
        memberfk("fk_submission_principal"),
        fk(
            "fk_submission_input",
            ["org_id", "repository_id", "input_object_id"],
            "object_records",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_submission_snapshot",
            ["org_id", "repository_id", "snapshot_id"],
            "source_snapshots",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_submission_retry",
            ["org_id", "repository_id", "retry_of_submission_id"],
            "source_submissions",
            ["org_id", "repository_id", "id"],
        ),
        ck("ck_submission_kind", "source_kind IN ('upload_zip','github_archive')"),
        ck(
            "ck_submission_state",
            "state IN "
            "('reserved','uploading','uploaded','admission_queued','admitting','admitted','rejected','failed','expired','cancelled')",
        ),
        ck("ck_submission_reservation", "reserved_bytes>=0"),
        ck(
            "ck_submission_terminal",
            "(state IN "
            "('admitted','rejected','failed','expired','cancelled'))=(finished_at IS NOT NULL)",
        ),
        ck("ck_submission_snapshot_shape", "(state='admitted')=(snapshot_id IS NOT NULL)"),
        ck(
            "ck_submission_input_shape",
            "num_nonnulls(input_object_id,actual_sha,actual_size) IN (0,3)",
        ),
    ],
    [
        ("ix_submission_history", ["org_id", "repository_id", "created_at", "id"]),
        ("ix_submission_expiry", ["state", "expires_at"]),
    ],
)
table(
    "inventory_entries",
    6,
    [
        orgid(),
        col("snapshot_id", "uuid"),
        col("file_id", "uuid"),
        col("eligible", "bool"),
        col("exclusion_reason", nullable=True),
        col("policy_hash"),
        created(),
        pk("org_id", "snapshot_id", "file_id"),
        fk(
            "fk_inventory_file",
            ["org_id", "snapshot_id", "file_id"],
            "source_files",
            ["org_id", "snapshot_id", "id"],
        ),
        ck("ck_inventory_eligibility", "eligible=(exclusion_reason IS NULL)"),
        ck(
            "ck_inventory_reason",
            "exclusion_reason IS NULL OR exclusion_reason IN "
            "('oversized','unsupported_language','binary','nested_archive','sensitive_path','generated_dependency')",
        ),
    ],
    [("ix_inventory_eligible", ["org_id", "snapshot_id", "eligible", "file_id"])],
)
table(
    "cleanup_tasks",
    6,
    [
        orgid(),
        ident(),
        col("object_id", "uuid"),
        col("reason"),
        col("state", default="'pending'"),
        col("claim_token", nullable=True),
        col("claim_expires_at", "time", True),
        col("attempt_count", "int", default="0"),
        col("next_attempt_at", "time", default="CURRENT_TIMESTAMP"),
        col("last_error_code", nullable=True),
        created(),
        pk("org_id", "id"),
        uq("uq_cleanup_reason", "org_id", "object_id", "reason"),
        fk("fk_cleanup_object", ["org_id", "object_id"], "object_records", ["org_id", "id"]),
        ck("ck_cleanup_state", "state IN ('pending','claimed','done','exhausted')"),
    ],
    [("ix_cleanup_due", ["state", "next_attempt_at"])],
)
jobs = table(
    "jobs",
    7,
    [
        orgid(),
        ident(),
        col("repository_id", "uuid"),
        col("principal_id", "uuid"),
        col("submission_id", "uuid", True),
        col("snapshot_id", "uuid", True),
        col("parent_job_id", "uuid", True),
        col("retry_of_job_id", "uuid", True),
        col("operation"),
        col("state", default="'queued'"),
        col("request_key"),
        col("request_hash"),
        col("config_hash"),
        col("dispatch_generation", "bigint", default="1"),
        col("fence", "bigint", default="0"),
        col("current_attempt_id", "uuid", True),
        col("result_run_id", "uuid", True),
        col("result_object_id", "uuid", True),
        col("cancel_requested_at", "time", True),
        col("attempt_count", "int", default="0"),
        col("max_attempts", "int", default="3"),
        col("not_before", "time", default="CURRENT_TIMESTAMP"),
        col("deadline", "time"),
        col("event_sequence", "bigint", default="0"),
        col("events_min_sequence", "bigint", default="1"),
        col("publication_checked_at", "time", True),
        col("failure_code", nullable=True),
        created(),
        col("finished_at", "time", True),
        pk("org_id", "id"),
        uq("uq_job_scope", "org_id", "repository_id", "id"),
        uq("uq_job_request", "org_id", "request_key"),
        repofk("fk_job_repo"),
        memberfk("fk_job_member"),
        fk(
            "fk_job_submission",
            ["org_id", "repository_id", "submission_id"],
            "source_submissions",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_job_snapshot",
            ["org_id", "repository_id", "snapshot_id"],
            "source_snapshots",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_job_parent",
            ["org_id", "repository_id", "parent_job_id"],
            "jobs",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_job_retry",
            ["org_id", "repository_id", "retry_of_job_id"],
            "jobs",
            ["org_id", "repository_id", "id"],
        ),
        fk(
            "fk_job_result_run",
            ["org_id", "snapshot_id", "result_run_id"],
            "analysis_runs",
            ["org_id", "snapshot_id", "id"],
        ),
        fk(
            "fk_job_result_object",
            ["org_id", "repository_id", "result_object_id"],
            "object_records",
            ["org_id", "repository_id", "id"],
        ),
        ck("ck_job_operation", "operation IN ('admit_source','inventory_snapshot')"),
        ck(
            "ck_job_state",
            "state IN "
            "('queued','running','retry_wait','cancelling','succeeded','partial','failed','cancelled')",
        ),
        ck(
            "ck_job_counters",
            "fence>=0 AND dispatch_generation>0 AND attempt_count>=0 AND max_attempts "
            "BETWEEN 1 AND 3",
        ),
        ck(
            "ck_job_terminal",
            "(state IN ('succeeded','partial','failed','cancelled'))=(finished_at IS NOT NULL)",
        ),
        ck(
            "ck_job_publication",
            "(state IN ('succeeded','partial'))=(publication_checked_at IS NOT NULL)",
        ),
        ck(
            "ck_job_inventory_snapshot",
            "operation<>'inventory_snapshot' OR snapshot_id IS NOT NULL",
        ),
    ],
    [
        ("ix_job_ready", ["state", "not_before", "org_id", "id"]),
        ("ix_job_expiry", ["state", "deadline"]),
        ("ix_job_org_active", ["org_id", "state"]),
    ],
)
attempts = table(
    "job_attempts",
    7,
    [
        orgid(),
        col("job_id", "uuid"),
        ident(),
        col("fence", "bigint"),
        col("owner"),
        col("state", default="'active'"),
        col("run_snapshot_id", "uuid", True),
        col("run_id", "uuid", True),
        col("lease_expires_at", "time"),
        col("heartbeat_at", "time"),
        col("started_at", "time", default="CURRENT_TIMESTAMP"),
        col("finished_at", "time", True),
        col("error_code", nullable=True),
        pk("org_id", "job_id", "id"),
        uq("uq_attempt_fence", "org_id", "job_id", "fence"),
        uq("uq_attempt_full", "org_id", "job_id", "id", "fence"),
        fk("fk_attempt_job", ["org_id", "job_id"], "jobs", ["org_id", "id"]),
        fk(
            "fk_attempt_run",
            ["org_id", "run_snapshot_id", "run_id"],
            "analysis_runs",
            ["org_id", "snapshot_id", "id"],
            True,
        ),
        ck("ck_attempt_state", "state IN ('active','succeeded','failed','expired','cancelled')"),
        ck("ck_attempt_run_pair", "(run_id IS NULL)=(run_snapshot_id IS NULL)"),
        ck("ck_attempt_terminal", "(state<>'active')=(finished_at IS NOT NULL)"),
        ck("ck_attempt_fence", "fence>0"),
    ],
    [("ix_attempt_expiry", ["state", "lease_expires_at"])],
)
sa.Index(
    "uq_attempt_active",
    attempts.c.org_id,
    attempts.c.job_id,
    unique=True,
    postgresql_where=sa.text("state='active'"),
)
table(
    "outbox",
    7,
    [
        orgid(),
        ident(),
        col("job_id", "uuid"),
        col("kind"),
        col("generation", "bigint"),
        *obj("payload"),
        col("state", default="'pending'"),
        col("claim_token", nullable=True),
        col("claim_expires_at", "time", True),
        col("send_attempts", "int", default="0"),
        col("next_send_at", "time", default="CURRENT_TIMESTAMP"),
        col("sent_at", "time", True),
        created(),
        pk("org_id", "id"),
        uq("uq_outbox_effect", "org_id", "job_id", "kind", "generation"),
        fk("fk_outbox_job", ["org_id", "job_id"], "jobs", ["org_id", "id"]),
        ck("ck_outbox_state", "state IN ('pending','claimed','sent')"),
        ck("ck_outbox_kind", "kind IN ('wakeup','dead_letter','memory_proposals')"),
    ],
    [("ix_outbox_due", ["state", "next_send_at"]), ("ix_outbox_claim", ["claim_expires_at"])],
)
table(
    "job_events",
    7,
    [
        orgid(),
        col("job_id", "uuid"),
        col("sequence", "bigint"),
        col("event_type"),
        col("run_snapshot_id", "uuid", True),
        col("run_id", "uuid", True),
        *obj("public_payload"),
        created(),
        pk("org_id", "job_id", "sequence"),
        fk("fk_event_job", ["org_id", "job_id"], "jobs", ["org_id", "id"]),
        fk(
            "fk_event_run",
            ["org_id", "run_snapshot_id", "run_id"],
            "analysis_runs",
            ["org_id", "snapshot_id", "id"],
        ),
        ck("ck_event_sequence", "sequence>0"),
        ck("ck_event_payload_size", "octet_length(public_payload::text)<=4096"),
    ],
    [("ix_events_created", ["created_at"])],
)
table(
    "webhook_receipts",
    7,
    [
        col("delivery_id"),
        col("event_type"),
        col("body_sha256"),
        col("state"),
        *obj("normalized_action"),
        col("received_at", "time", default="CURRENT_TIMESTAMP"),
        col("processed_at", "time", True),
        pk("delivery_id"),
        ck("ck_webhook_state", "state IN ('received','processed')"),
    ],
    [("ix_webhook_received", ["received_at"])],
)

table(
    "api_commands",
    9,
    [
        orgid(),
        col("principal_id", "uuid"),
        col("request_key"),
        col("operation"),
        col("request_hash"),
        col("response", "json"),
        created(),
        pk("org_id", "principal_id", "request_key"),
        memberfk("fk_command_membership"),
        ck("ck_command_key", "octet_length(request_key) BETWEEN 1 AND 128"),
        ck("ck_command_hash", "request_hash ~ '^[0-9a-f]{64}$'"),
        ck(
            "ck_command_operation",
            "operation IN ('repository_create','github_bind','finding_triage')",
        ),
        ck(
            "ck_command_response",
            "jsonb_typeof(response)='object' AND octet_length(response::text)<=65536",
        ),
    ],
)

PHASE2_TABLES = {name: t for name, _, t in spec}

# Deferred circular associations are installed after both parents exist.
jobs.append_constraint(
    fk(
        "fk_job_current_attempt",
        ["org_id", "id", "current_attempt_id"],
        "job_attempts",
        ["org_id", "job_id", "id"],
    )
)
objects.append_constraint(
    fk(
        "fk_object_job",
        ["org_id", "repository_id", "owner_job_id"],
        "jobs",
        ["org_id", "repository_id", "id"],
    )
)
for name in ["worker_job_id", "worker_attempt_id"]:
    legacy.analysis_runs.append_column(sa.Column(name, UUID(as_uuid=True), nullable=True))
legacy.analysis_runs.append_column(sa.Column("worker_fence", sa.BigInteger(), nullable=True))
legacy.analysis_runs.append_constraint(
    ck(
        "ck_run_worker_binding",
        "num_nonnulls(worker_job_id,worker_attempt_id,worker_fence) IN (0,3)",
    )
)
legacy.analysis_runs.append_constraint(
    fk(
        "fk_run_worker_attempt",
        ["org_id", "worker_job_id", "worker_attempt_id", "worker_fence"],
        "job_attempts",
        ["org_id", "job_id", "id", "fence"],
    )
)

# Closed identity/size bounds apply even when writes bypass transport validation.
for _name, _, _table in spec:
    for _column in _table.columns:
        if _column.name in {
            "sha256",
            "declared_sha",
            "actual_sha",
            "policy_hash",
            "request_hash",
            "config_hash",
            "body_sha256",
            "state_hash",
            "browser_hash",
            "token_hash",
            "csrf_hash",
        }:
            _table.append_constraint(
                ck(f"ck_{_name}_{_column.name}", f"{_column.name} ~ '^[0-9a-f]{{64}}$'")
            )
        elif isinstance(_column.type, sa.Text):
            _bound = 1024 if _column.name in {"key", "return_path", "requested_ref"} else 255
            _table.append_constraint(
                ck(
                    f"ck_{_name}_{_column.name}_bytes",
                    f"octet_length({_column.name}) BETWEEN 1 AND {_bound}",
                )
            )
        if _column.name in {
            "size_bytes",
            "actual_size",
            "declared_size",
            "attempt_count",
            "send_attempts",
        }:
            _table.append_constraint(
                ck(f"ck_{_name}_{_column.name}_nonnegative", f"{_column.name}>=0")
            )
