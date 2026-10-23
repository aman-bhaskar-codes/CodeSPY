"""Model gateway capabilities, generation price schedules, and reservation ledger."""

from alembic import op

revision = "0020_model_gateway_pricing"
down_revision = "0019_repo_memory"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

ALTER TABLE public.price_schedules
    ADD COLUMN IF NOT EXISTS output_micros_per_1k BIGINT DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS thinking_micros_per_1k BIGINT DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS cache_read_micros_per_1k BIGINT DEFAULT 0 NOT NULL,
    ADD COLUMN IF NOT EXISTS cache_write_micros_per_1k BIGINT DEFAULT 0 NOT NULL;

CREATE TABLE public.model_capabilities (
    provider VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    revision VARCHAR(32) NOT NULL,
    capability_type VARCHAR(32) NOT NULL,
    max_context_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    supports_tools BOOLEAN DEFAULT FALSE NOT NULL,
    supports_structured_output BOOLEAN DEFAULT FALSE NOT NULL,
    supports_streaming BOOLEAN DEFAULT TRUE NOT NULL,
    supports_thinking BOOLEAN DEFAULT FALSE NOT NULL,
    geographic_regions JSONB DEFAULT '["us-east-1"]'::jsonb NOT NULL,
    requires_disclosure_approval BOOLEAN DEFAULT TRUE NOT NULL,
    default_price_schedule_version VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (provider, model_id, revision)
);

CREATE TABLE public.model_reservations (
    org_id UUID NOT NULL,
    reservation_id UUID NOT NULL,
    repository_id UUID NOT NULL,
    principal_id UUID NOT NULL,
    investigation_id UUID NOT NULL,
    logical_call_id UUID NOT NULL,
    attempt_id INTEGER NOT NULL,
    provider VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    charge_category VARCHAR(32) DEFAULT 'generation' NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    estimated_output_tokens INTEGER NOT NULL,
    reserved_micros BIGINT NOT NULL,
    settled_micros BIGINT DEFAULT NULL,
    state VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    provider_request_id VARCHAR(128) DEFAULT NULL,
    failure_reason TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, reservation_id),
    CONSTRAINT uq_model_reservation_idempotency UNIQUE (org_id, idempotency_key),
    CONSTRAINT ck_model_reservation_state CHECK (
        state IN (
            'reserved',
            'dispatch_intent_committed',
            'dispatched',
            'settled',
            'rejected_before_dispatch',
            'uncertain_hold',
            'conservatively_reconciled'
        )
    ),
    CONSTRAINT ck_model_reservation_micros_pos CHECK (reserved_micros >= 0),
    CONSTRAINT fk_model_reservation_member FOREIGN KEY (org_id, principal_id)
        REFERENCES public.memberships (org_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT fk_model_reservation_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_model_reservations_lookup ON public.model_reservations (
    org_id, repository_id, investigation_id, state
);

CREATE TABLE public.model_spend_ledger (
    org_id UUID NOT NULL,
    id UUID NOT NULL,
    repository_id UUID NOT NULL,
    reservation_id UUID NOT NULL,
    investigation_id UUID NOT NULL,
    attempt_id INTEGER NOT NULL,
    provider VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    charge_category VARCHAR(32) NOT NULL,
    input_tokens INTEGER DEFAULT 0 NOT NULL,
    output_tokens INTEGER DEFAULT 0 NOT NULL,
    thinking_tokens INTEGER DEFAULT 0 NOT NULL,
    charged_micros BIGINT NOT NULL,
    is_uncertain_hold BOOLEAN DEFAULT FALSE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, id),
    CONSTRAINT fk_spend_ledger_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_model_spend_ledger_investigation ON public.model_spend_ledger (
    org_id, investigation_id
);

GRANT SELECT, INSERT, UPDATE, DELETE ON public.model_capabilities TO codespy_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.model_reservations TO codespy_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.model_spend_ledger TO codespy_app;
"""

DOWN_SQL = r"""
SET LOCAL lock_timeout='5s';
DROP TABLE IF EXISTS public.model_spend_ledger CASCADE;
DROP TABLE IF EXISTS public.model_reservations CASCADE;
DROP TABLE IF EXISTS public.model_capabilities CASCADE;
ALTER TABLE public.price_schedules
    DROP COLUMN IF EXISTS output_micros_per_1k,
    DROP COLUMN IF EXISTS thinking_micros_per_1k,
    DROP COLUMN IF EXISTS cache_read_micros_per_1k,
    DROP COLUMN IF EXISTS cache_write_micros_per_1k;
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN_SQL)
