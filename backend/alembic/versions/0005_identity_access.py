# ruff: noqa: E501 -- frozen SQL constraints remain legible as complete statements
"""Frozen additive identity access schema."""

from alembic import op

revision = "0005_identity_access"
down_revision = "0004_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE external_identities (
	provider TEXT NOT NULL,
	subject TEXT NOT NULL,
	principal_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (provider, subject),
	CONSTRAINT uq_identity_principal UNIQUE (provider, principal_id),
	CONSTRAINT fk_external_principal FOREIGN KEY(principal_id) REFERENCES principals (id) ON DELETE RESTRICT,
	CONSTRAINT ck_external_identities_provider_bytes CHECK (octet_length(provider) BETWEEN 1 AND 255),
	CONSTRAINT ck_external_identities_subject_bytes CHECK (octet_length(subject) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE TABLE oauth_flows (
	id UUID NOT NULL,
	state_hash TEXT NOT NULL,
	browser_hash TEXT NOT NULL,
	verifier_cipher BYTEA NOT NULL,
	key_id TEXT NOT NULL,
	return_path TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	consumed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_oauth_state UNIQUE (state_hash),
	CONSTRAINT ck_oauth_flows_state_hash CHECK (state_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_oauth_flows_browser_hash CHECK (browser_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_oauth_flows_key_id_bytes CHECK (octet_length(key_id) BETWEEN 1 AND 255),
	CONSTRAINT ck_oauth_flows_return_path_bytes CHECK (octet_length(return_path) BETWEEN 1 AND 1024)
)
""")
    op.execute("""
CREATE INDEX ix_oauth_expiry ON oauth_flows (expires_at)
""")
    op.execute("""
CREATE TABLE sessions (
	id UUID NOT NULL,
	principal_id UUID NOT NULL,
	token_hash TEXT NOT NULL,
	csrf_hash TEXT NOT NULL,
	issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	idle_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	absolute_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_session_token UNIQUE (token_hash),
	CONSTRAINT fk_session_principal FOREIGN KEY(principal_id) REFERENCES principals (id) ON DELETE RESTRICT,
	CONSTRAINT ck_sessions_token_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_sessions_csrf_hash CHECK (csrf_hash ~ '^[0-9a-f]{64}$')
)
""")
    op.execute("""
CREATE INDEX ix_session_expiry ON sessions (absolute_expires_at)
""")
    op.execute("""
CREATE INDEX ix_session_principal ON sessions (principal_id, revoked_at)
""")
    op.execute("""
CREATE TABLE github_user_grants (
	principal_id UUID NOT NULL,
	token_cipher BYTEA NOT NULL,
	key_id TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	version BIGINT DEFAULT 0 NOT NULL,
	last_verified_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (principal_id),
	CONSTRAINT fk_github_principal FOREIGN KEY(principal_id) REFERENCES principals (id) ON DELETE RESTRICT,
	CONSTRAINT ck_github_grant_version CHECK (version>=0 AND length(key_id)>0),
	CONSTRAINT ck_github_user_grants_key_id_bytes CHECK (octet_length(key_id) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE TABLE repository_grants (
	org_id UUID NOT NULL,
	repository_id UUID NOT NULL,
	principal_id UUID NOT NULL,
	permission TEXT NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id, repository_id, principal_id),
	CONSTRAINT fk_grant_repository FOREIGN KEY(org_id, repository_id) REFERENCES repositories (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_grant_membership FOREIGN KEY(org_id, principal_id) REFERENCES memberships (org_id, principal_id) ON DELETE RESTRICT,
	CONSTRAINT ck_repo_permission CHECK (permission IN ('read','write')),
	CONSTRAINT ck_repository_grants_permission_bytes CHECK (octet_length(permission) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_grant_principal ON repository_grants (org_id, principal_id, repository_id)
""")
    op.execute("""
CREATE TABLE github_repository_bindings (
	org_id UUID NOT NULL,
	repository_id UUID NOT NULL,
	github_repository_id BIGINT NOT NULL,
	installation_id BIGINT NOT NULL,
	account_id BIGINT NOT NULL,
	state TEXT NOT NULL,
	version BIGINT DEFAULT 0 NOT NULL,
	last_checked_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (org_id, repository_id),
	CONSTRAINT uq_binding_github UNIQUE (org_id, github_repository_id),
	CONSTRAINT fk_binding_repo FOREIGN KEY(org_id, repository_id) REFERENCES repositories (org_id, id) ON DELETE RESTRICT,
	CONSTRAINT ck_binding_ids CHECK (github_repository_id>0 AND installation_id>0 AND account_id>0 AND version>=0),
	CONSTRAINT ck_binding_state CHECK (state IN ('active','suspended','revoked')),
	CONSTRAINT ck_github_repository_bindings_state_bytes CHECK (octet_length(state) BETWEEN 1 AND 255)
)
""")
    op.execute("""
CREATE INDEX ix_binding_installation ON github_repository_bindings (installation_id)
""")
    op.execute("""
CREATE TABLE org_work_limits (
	org_id UUID NOT NULL,
	max_active_jobs INTEGER DEFAULT 2 NOT NULL,
	max_pending_jobs INTEGER DEFAULT 20 NOT NULL,
	max_reserved_bytes BIGINT DEFAULT 1073741824 NOT NULL,
	reserved_bytes BIGINT DEFAULT 0 NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (org_id),
	CONSTRAINT fk_limits_org FOREIGN KEY(org_id) REFERENCES organizations (id) ON DELETE RESTRICT,
	CONSTRAINT ck_work_limits CHECK (max_active_jobs>0 AND max_pending_jobs>0 AND reserved_bytes>=0 AND reserved_bytes<=max_reserved_bytes)
)
""")


def downgrade() -> None:
    op.drop_table("org_work_limits")
    op.drop_table("github_repository_bindings")
    op.drop_table("repository_grants")
    op.drop_table("github_user_grants")
    op.drop_table("sessions")
    op.drop_table("oauth_flows")
    op.drop_table("external_identities")
