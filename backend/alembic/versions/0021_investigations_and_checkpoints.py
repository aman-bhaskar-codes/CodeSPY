# ruff: noqa: E501
"""Investigation domain tables: investigations, fenced checkpoints, and append-only event stream."""

from alembic import op

revision = "0021_investigations_and_checkpoints"
down_revision = "0020_model_gateway_pricing"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.investigations (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    principal_id UUID NOT NULL,
    finding_id UUID DEFAULT NULL,
    target_type VARCHAR(32) NOT NULL,
    query TEXT NOT NULL,
    status VARCHAR(32) DEFAULT 'pending' NOT NULL,
    mode VARCHAR(32) DEFAULT 'short_path' NOT NULL,
    depth VARCHAR(32) DEFAULT 'standard' NOT NULL,
    current_step INTEGER DEFAULT 0 NOT NULL,
    max_steps INTEGER DEFAULT 10 NOT NULL,
    fencing_token BIGINT DEFAULT 1 NOT NULL,
    worker_id VARCHAR(128) DEFAULT NULL,
    worker_lease_expires_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    budget_limit_micros BIGINT NOT NULL,
    spent_micros BIGINT DEFAULT 0 NOT NULL,
    verdict VARCHAR(32) DEFAULT NULL,
    verdict_confidence DOUBLE PRECISION DEFAULT NULL,
    summary TEXT DEFAULT NULL,
    failure_reason TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT ck_investigation_status CHECK (
        status IN ('pending', 'running', 'checkpointed', 'awaiting_human_decision', 'completed', 'failed', 'cancelled')
    ),
    CONSTRAINT ck_investigation_target_type CHECK (
        target_type IN ('finding', 'repository', 'commit', 'diff')
    ),
    CONSTRAINT ck_investigation_mode CHECK (
        mode IN ('short_path', 'two_specialist')
    ),
    CONSTRAINT ck_investigation_depth CHECK (
        depth IN ('quick', 'standard', 'deep')
    ),
    CONSTRAINT ck_investigation_verdict CHECK (
        verdict IS NULL OR verdict IN ('true_positive', 'false_positive', 'indeterminate', 'benign')
    ),
    CONSTRAINT ck_investigation_budget_pos CHECK (budget_limit_micros > 0),
    CONSTRAINT ck_investigation_spent_nonneg CHECK (spent_micros >= 0),
    CONSTRAINT ck_investigation_steps CHECK (current_step >= 0 AND max_steps > 0 AND current_step <= max_steps + 1),
    CONSTRAINT ck_investigation_fencing_pos CHECK (fencing_token >= 1),
    CONSTRAINT fk_investigation_repo FOREIGN KEY (org_id, repository_id) REFERENCES public.repositories (org_id, id),
    CONSTRAINT fk_investigation_member FOREIGN KEY (org_id, principal_id) REFERENCES public.memberships (org_id, principal_id)
);

CREATE INDEX idx_investigations_repo_status ON public.investigations (org_id, repository_id, status, created_at DESC);
CREATE INDEX idx_investigations_finding ON public.investigations (org_id, finding_id) WHERE finding_id IS NOT NULL;

CREATE TABLE public.investigation_checkpoints (
    org_id UUID NOT NULL,
    investigation_id UUID NOT NULL,
    step_index INTEGER NOT NULL,
    fencing_token BIGINT NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    graph_node VARCHAR(64) NOT NULL,
    state_snapshot_json JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, investigation_id, step_index),
    CONSTRAINT ck_checkpoint_step_nonneg CHECK (step_index >= 0),
    CONSTRAINT ck_checkpoint_fencing_pos CHECK (fencing_token >= 1),
    CONSTRAINT fk_checkpoint_investigation FOREIGN KEY (org_id, investigation_id)
        REFERENCES public.investigations (org_id, id) ON DELETE CASCADE
);

CREATE TABLE public.investigation_events (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    investigation_id UUID NOT NULL,
    step_index INTEGER NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    node_name VARCHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT ck_event_step_nonneg CHECK (step_index >= 0),
    CONSTRAINT fk_event_investigation FOREIGN KEY (org_id, investigation_id)
        REFERENCES public.investigations (org_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_investigation_events_stream ON public.investigation_events (org_id, investigation_id, step_index, created_at ASC);
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS public.investigation_events CASCADE;
        DROP TABLE IF EXISTS public.investigation_checkpoints CASCADE;
        DROP TABLE IF EXISTS public.investigations CASCADE;
        """
    )
