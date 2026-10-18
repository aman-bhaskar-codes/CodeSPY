# ruff: noqa: E501 -- frozen SQL constraints remain legible as complete statements
"""Frozen additive jobs events schema."""

from alembic import op

revision = "0007_jobs_events"
down_revision = "0006_submission_objects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE jobs (
	org_id UUID NOT NULL,
	id UUID NOT NULL,
	repository_id UUID NOT NULL,
	principal_id UUID NOT NULL,
	submission_id UUID,
	snapshot_id UUID,
	parent_job_id UUID,
	retry_of_job_id UUID,
	operation TEXT NOT NULL,
	state TEXT DEFAULT 'queued' NOT NULL,
	request_key TEXT NOT NULL,
	request_hash TEXT NOT NULL,
	config_hash TEXT NOT NULL,
	dispatch_generation BIGINT DEFAULT 1 NOT NULL,
	fence BIGINT DEFAULT 0 NOT NULL,
	current_attempt_id UUID,
	result_run_id UUID,
	result_object_id UUID,
	cancel_requested_at TIMESTAMP WITH TIME ZONE,
	attempt_count INTEGER DEFAULT 0 NOT NULL,
	max_attempts INTEGER DEFAULT 3 NOT NULL,
	not_before TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	event_sequence BIGINT DEFAULT 0 NOT NULL,
	events_min_sequence BIGINT DEFAULT 1 NOT NULL,
	publication_checked_at TIMESTAMP WITH TIME ZONE,
	failure_code TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (org_id, id),
	CONSTRAINT uq_job_scope UNIQUE (org_id, repository_id, id),
	CONSTRAINT uq_job_request UNIQUE (org_id, request_key),
	CONSTRAINT fk_job_repo FOREIGN KEY(org_id, repository_id) REFERENCES repositories (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_member FOREIGN KEY(org_id, principal_id) REFERENCES memberships (org_id, principal_id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_submission FOREIGN KEY(org_id, repository_id, submission_id) REFERENCES source_submissions (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_snapshot FOREIGN KEY(org_id, repository_id, snapshot_id) REFERENCES source_snapshots (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_parent FOREIGN KEY(org_id, repository_id, parent_job_id) REFERENCES jobs (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_retry FOREIGN KEY(org_id, repository_id, retry_of_job_id) REFERENCES jobs (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_result_run FOREIGN KEY(org_id, snapshot_id, result_run_id) REFERENCES analysis_runs (org_id, snapshot_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_job_result_object FOREIGN KEY(org_id, repository_id, result_object_id) REFERENCES object_records (org_id, repository_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_job_operation CHECK (operation IN ('admit_source','inventory_snapshot')),
	CONSTRAINT ck_job_state CHECK (state IN ('queued','running','retry_wait','cancelling','succeeded','partial','failed','cancelled')),
	CONSTRAINT ck_job_counters CHECK (fence>=0 AND dispatch_generation>0 AND attempt_count>=0 AND max_attempts BETWEEN 1 AND 3),
	CONSTRAINT ck_job_terminal CHECK ((state IN ('succeeded','partial','failed','cancelled'))=(finished_at IS NOT NULL)),
	CONSTRAINT ck_job_publication CHECK ((state IN ('succeeded','partial'))=(publication_checked_at IS NOT NULL)),
	CONSTRAINT ck_job_inventory_snapshot CHECK (operation<>'inventory_snapshot' OR snapshot_id IS NOT NULL),
	CONSTRAINT ck_jobs_operation_bytes CHECK (octet_length(operation) BETWEEN 1 AND 255),
	CONSTRAINT ck_jobs_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_jobs_request_key_bytes CHECK (octet_length(request_key) BETWEEN 1 AND 255),
	CONSTRAINT ck_jobs_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_jobs_config_hash CHECK (config_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_jobs_attempt_count_nonnegative CHECK (attempt_count>=0),
	CONSTRAINT ck_jobs_failure_code_bytes CHECK (octet_length(failure_code) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_job_expiry ON jobs (state, deadline)
""")
    op.execute("""
CREATE INDEX ix_job_org_active ON jobs (org_id, state)
""")
    op.execute("""
CREATE INDEX ix_job_ready ON jobs (state, not_before, org_id, id)
""")
    op.execute("""
CREATE TABLE job_attempts (
	org_id UUID NOT NULL,
	job_id UUID NOT NULL,
	id UUID NOT NULL,
	fence BIGINT NOT NULL,
	owner TEXT NOT NULL,
	state TEXT DEFAULT 'active' NOT NULL,
	run_snapshot_id UUID,
	run_id UUID,
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	error_code TEXT,
	PRIMARY KEY (org_id, job_id, id),
	CONSTRAINT uq_attempt_fence UNIQUE (org_id, job_id, fence),
	CONSTRAINT uq_attempt_full UNIQUE (org_id, job_id, id, fence),
	CONSTRAINT fk_attempt_job FOREIGN KEY(org_id, job_id) REFERENCES jobs (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_attempt_run FOREIGN KEY(org_id, run_snapshot_id, run_id) REFERENCES analysis_runs (org_id, snapshot_id, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT ck_attempt_state CHECK (state IN ('active','succeeded','failed','expired','cancelled')),
	CONSTRAINT ck_attempt_run_pair CHECK ((run_id IS NULL)=(run_snapshot_id IS NULL)),
	CONSTRAINT ck_attempt_terminal CHECK ((state<>'active')=(finished_at IS NOT NULL)),
	CONSTRAINT ck_attempt_fence CHECK (fence>0),
	CONSTRAINT ck_job_attempts_owner_bytes CHECK (octet_length(owner) BETWEEN 1 AND 255),
	CONSTRAINT ck_job_attempts_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_job_attempts_error_code_bytes CHECK (octet_length(error_code) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_attempt_expiry ON job_attempts (state, lease_expires_at)
""")
    op.execute("""
CREATE UNIQUE INDEX uq_attempt_active ON job_attempts (org_id, job_id) WHERE state='active'
""")
    op.execute("""
CREATE TABLE outbox (
	org_id UUID NOT NULL,
	id UUID NOT NULL,
	job_id UUID NOT NULL,
	kind TEXT NOT NULL,
	generation BIGINT NOT NULL,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	state TEXT DEFAULT 'pending' NOT NULL,
	claim_token TEXT,
	claim_expires_at TIMESTAMP WITH TIME ZONE,
	send_attempts INTEGER DEFAULT 0 NOT NULL,
	next_send_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	sent_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, id),
	CONSTRAINT ck_payload_object CHECK (jsonb_typeof(payload)='object'),
	CONSTRAINT uq_outbox_effect UNIQUE (org_id, job_id, kind, generation),
	CONSTRAINT fk_outbox_job FOREIGN KEY(org_id, job_id) REFERENCES jobs (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_outbox_state CHECK (state IN ('pending','claimed','sent')),
	CONSTRAINT ck_outbox_kind CHECK (kind IN ('wakeup','dead_letter')),
	CONSTRAINT ck_outbox_kind_bytes CHECK (octet_length(kind) BETWEEN 1 AND 255),
	CONSTRAINT ck_outbox_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255),
	CONSTRAINT ck_outbox_claim_token_bytes CHECK (octet_length(claim_token) BETWEEN 1 AND 255),
	CONSTRAINT ck_outbox_send_attempts_nonnegative CHECK (send_attempts>=0)
)
""")
    op.execute("""
CREATE INDEX ix_outbox_claim ON outbox (claim_expires_at)
""")
    op.execute("""
CREATE INDEX ix_outbox_due ON outbox (state, next_send_at)
""")
    op.execute("""
CREATE TABLE job_events (
	org_id UUID NOT NULL,
	job_id UUID NOT NULL,
	sequence BIGINT NOT NULL,
	event_type TEXT NOT NULL,
	run_snapshot_id UUID,
	run_id UUID,
	public_payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, job_id, sequence),
	CONSTRAINT ck_public_payload_object CHECK (jsonb_typeof(public_payload)='object'),
	CONSTRAINT fk_event_job FOREIGN KEY(org_id, job_id) REFERENCES jobs (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_event_run FOREIGN KEY(org_id, run_snapshot_id, run_id) REFERENCES analysis_runs (org_id, snapshot_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_event_sequence CHECK (sequence>0),
	CONSTRAINT ck_event_payload_size CHECK (octet_length(public_payload::text)<=4096),
	CONSTRAINT ck_job_events_event_type_bytes CHECK (octet_length(event_type) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_events_created ON job_events (created_at)
""")
    op.execute("""
CREATE TABLE webhook_receipts (
	delivery_id TEXT NOT NULL,
	event_type TEXT NOT NULL,
	body_sha256 TEXT NOT NULL,
	state TEXT NOT NULL,
	normalized_action JSONB DEFAULT '{}'::jsonb NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (delivery_id),
	CONSTRAINT ck_normalized_action_object CHECK (jsonb_typeof(normalized_action)='object'),
	CONSTRAINT ck_webhook_state CHECK (state IN ('received','processed')),
	CONSTRAINT ck_webhook_receipts_delivery_id_bytes CHECK (octet_length(delivery_id) BETWEEN 1 AND 255),
	CONSTRAINT ck_webhook_receipts_event_type_bytes CHECK (octet_length(event_type) BETWEEN 1 AND 255),
	CONSTRAINT ck_webhook_receipts_body_sha256 CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_webhook_receipts_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_webhook_received ON webhook_receipts (received_at)
""")
    op.execute("""
ALTER TABLE jobs ADD CONSTRAINT fk_job_current_attempt FOREIGN KEY(org_id,id,current_attempt_id) REFERENCES job_attempts(org_id,job_id,id) ON DELETE RESTRICT
""")
    op.execute("""
ALTER TABLE object_records ADD CONSTRAINT fk_object_job FOREIGN KEY(org_id,repository_id,owner_job_id) REFERENCES jobs(org_id,repository_id,id) ON DELETE RESTRICT
""")


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP CONSTRAINT fk_job_current_attempt")
    op.execute("ALTER TABLE object_records DROP CONSTRAINT fk_object_job")
    op.drop_table("webhook_receipts")
    op.drop_table("job_events")
    op.drop_table("outbox")
    op.drop_table("job_attempts")
    op.drop_table("jobs")
