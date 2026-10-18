# ruff: noqa: E501 -- frozen SQL constraints remain legible as complete statements
"""Frozen additive submission objects schema."""

from alembic import op

revision = "0006_submission_objects"
down_revision = "0005_identity_access"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE object_records (
	org_id UUID NOT NULL,
	id UUID NOT NULL,
	repository_id UUID NOT NULL,
	key TEXT NOT NULL,
	kind TEXT NOT NULL,
	state TEXT NOT NULL,
	sha256 TEXT,
	size_bytes BIGINT,
	version_id TEXT,
	owner_job_id UUID,
	owner_fence BIGINT,
	expires_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, id),
	CONSTRAINT uq_object_scope UNIQUE (org_id, repository_id, id),
	CONSTRAINT uq_object_key UNIQUE (org_id, repository_id, key),
	CONSTRAINT fk_object_repo FOREIGN KEY(org_id, repository_id) REFERENCES repositories (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_object_kind CHECK (kind IN ('input','stage','source','manifest')),
	CONSTRAINT ck_object_state CHECK (state IN ('reserved','writing','quarantined','verified','available','deleting','deleted')),
	CONSTRAINT ck_object_digest CHECK (state NOT IN ('verified','available') OR (sha256 ~ '^[0-9a-f]{64}$' AND sha256 IS NOT NULL AND size_bytes>=0 AND size_bytes IS NOT NULL)),
	CONSTRAINT ck_object_owner CHECK ((owner_job_id IS NULL)=(owner_fence IS NULL)),
	CONSTRAINT ck_object_records_key_bytes CHECK (octet_length(key) BETWEEN 1 AND 1024),
	CONSTRAINT ck_object_records_kind_bytes CHECK (octet_length(kind) BETWEEN 1 AND 255),
	CONSTRAINT ck_object_records_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_object_records_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_object_records_size_bytes_nonnegative CHECK (size_bytes>=0),
	CONSTRAINT ck_object_records_version_id_bytes CHECK (octet_length(version_id) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_object_expiry ON object_records (state, expires_at)
""")
    op.execute("""
CREATE TABLE source_submissions (
	org_id UUID NOT NULL,
	id UUID NOT NULL,
	repository_id UUID NOT NULL,
	principal_id UUID NOT NULL,
	source_kind TEXT NOT NULL,
	state TEXT NOT NULL,
	request_key TEXT NOT NULL,
	request_hash TEXT NOT NULL,
	retry_of_submission_id UUID,
	reserved_bytes BIGINT NOT NULL,
	reservation_released_at TIMESTAMP WITH TIME ZONE,
	finished_at TIMESTAMP WITH TIME ZONE,
	declared_size BIGINT,
	declared_sha TEXT,
	actual_size BIGINT,
	actual_sha TEXT,
	input_object_id UUID,
	snapshot_id UUID,
	requested_ref TEXT,
	resolved_commit TEXT,
	policy_hash TEXT NOT NULL,
	error_code TEXT,
	upload_owner TEXT,
	upload_expires_at TIMESTAMP WITH TIME ZONE,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, id),
	CONSTRAINT uq_submission_scope UNIQUE (org_id, repository_id, id),
	CONSTRAINT uq_submission_key UNIQUE (org_id, request_key),
	CONSTRAINT fk_submission_repo FOREIGN KEY(org_id, repository_id) REFERENCES repositories (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_submission_principal FOREIGN KEY(org_id, principal_id) REFERENCES memberships (org_id, principal_id) ON DELETE RESTRICT,
	CONSTRAINT fk_submission_input FOREIGN KEY(org_id, repository_id, input_object_id) REFERENCES object_records (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_submission_snapshot FOREIGN KEY(org_id, repository_id, snapshot_id) REFERENCES source_snapshots (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_submission_retry FOREIGN KEY(org_id, repository_id, retry_of_submission_id) REFERENCES source_submissions (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_submission_kind CHECK (source_kind IN ('upload_zip','github_archive')),
	CONSTRAINT ck_submission_state CHECK (state IN ('reserved','uploading','uploaded','admission_queued','admitting','admitted','rejected','failed','expired','cancelled')),
	CONSTRAINT ck_submission_reservation CHECK (reserved_bytes>=0),
	CONSTRAINT ck_submission_terminal CHECK ((state IN ('admitted','rejected','failed','expired','cancelled'))=(finished_at IS NOT NULL)),
	CONSTRAINT ck_submission_snapshot_shape CHECK ((state='admitted')=(snapshot_id IS NOT NULL)),
	CONSTRAINT ck_submission_input_shape CHECK (num_nonnulls(input_object_id,actual_sha,actual_size) IN (0,3)),
	CONSTRAINT ck_source_submissions_source_kind_bytes CHECK (octet_length(source_kind) BETWEEN 1 AND 255),
	CONSTRAINT ck_source_submissions_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_source_submissions_request_key_bytes CHECK (octet_length(request_key) BETWEEN 1 AND 255),
	CONSTRAINT ck_source_submissions_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_source_submissions_declared_size_nonnegative CHECK (declared_size>=0),
	CONSTRAINT ck_source_submissions_declared_sha CHECK (declared_sha ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_source_submissions_actual_size_nonnegative CHECK (actual_size>=0),
	CONSTRAINT ck_source_submissions_actual_sha CHECK (actual_sha ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_source_submissions_requested_ref_bytes CHECK (octet_length(requested_ref) BETWEEN 1 AND 1024),
	CONSTRAINT ck_source_submissions_resolved_commit_bytes CHECK (octet_length(resolved_commit) BETWEEN 1 AND 255),
	CONSTRAINT ck_source_submissions_policy_hash CHECK (policy_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_source_submissions_error_code_bytes CHECK (octet_length(error_code) BETWEEN 1 AND 255),
	CONSTRAINT ck_source_submissions_upload_owner_bytes CHECK (octet_length(upload_owner) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_submission_expiry ON source_submissions (state, expires_at)
""")
    op.execute("""
CREATE INDEX ix_submission_history ON source_submissions (org_id, repository_id, created_at, id)
""")
    op.execute("""
CREATE TABLE inventory_entries (
	org_id UUID NOT NULL,
	snapshot_id UUID NOT NULL,
	file_id UUID NOT NULL,
	eligible BOOLEAN NOT NULL,
	exclusion_reason TEXT,
	policy_hash TEXT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, snapshot_id, file_id),
	CONSTRAINT fk_inventory_file FOREIGN KEY(org_id, snapshot_id, file_id) REFERENCES source_files (org_id, snapshot_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_inventory_eligibility CHECK (eligible=(exclusion_reason IS NULL)),
	CONSTRAINT ck_inventory_reason CHECK (exclusion_reason IS NULL OR exclusion_reason IN ('oversized','unsupported_language','binary','nested_archive','sensitive_path','generated_dependency')),
	CONSTRAINT ck_inventory_entries_exclusion_reason_bytes CHECK (octet_length(exclusion_reason) BETWEEN 1 AND 255),
	CONSTRAINT ck_inventory_entries_policy_hash CHECK (policy_hash ~ '^[0-9a-f]{64}$')
)
""")
    op.execute("""
CREATE INDEX ix_inventory_eligible ON inventory_entries (org_id, snapshot_id, eligible, file_id)
""")
    op.execute("""
CREATE TABLE cleanup_tasks (
	org_id UUID NOT NULL,
	id UUID NOT NULL,
	object_id UUID NOT NULL,
	reason TEXT NOT NULL,
	state TEXT DEFAULT 'pending' NOT NULL,
	claim_token TEXT,
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	next_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_error_code TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, id),
	CONSTRAINT uq_cleanup_reason UNIQUE (org_id, object_id, reason),
	CONSTRAINT fk_cleanup_object FOREIGN KEY(org_id, object_id) REFERENCES object_records (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_cleanup_state CHECK (state IN ('pending','claimed','done','exhausted')),
	CONSTRAINT ck_cleanup_tasks_reason_bytes CHECK (octet_length(reason) BETWEEN 1 AND 255),
	CONSTRAINT ck_cleanup_tasks_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_cleanup_tasks_claim_token_bytes CHECK (octet_length(claim_token) BETWEEN 1 AND 255),
	CONSTRAINT ck_cleanup_tasks_attempt_count_nonnegative CHECK (attempt_count>=0),
	CONSTRAINT ck_cleanup_tasks_last_error_code_bytes CHECK (octet_length(last_error_code) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_cleanup_due ON cleanup_tasks (state, next_attempt_at)
""")


def downgrade() -> None:
    op.drop_table("cleanup_tasks")
    op.drop_table("inventory_entries")
    op.drop_table("source_submissions")
    op.drop_table("object_records")
