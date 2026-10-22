# ruff: noqa: E501
"""Provider disclosure policy, monetary units, and embedding budget reservation ledger."""

from alembic import op

revision = "0015_provider_budget"
down_revision = "0014_finding_triage"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.embedding_budgets (
    org_id UUID NOT NULL,
    repository_id UUID NOT NULL,
    currency VARCHAR(3) DEFAULT 'USD' NOT NULL,
    balance_micros BIGINT NOT NULL,
    reserved_micros BIGINT DEFAULT 0 NOT NULL,
    write_version BIGINT DEFAULT 1 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, repository_id),
    CONSTRAINT ck_budget_balance_nonneg CHECK (balance_micros >= 0),
    CONSTRAINT ck_budget_reserved_nonneg CHECK (reserved_micros >= 0),
    CONSTRAINT ck_budget_reserved_le_balance CHECK (reserved_micros <= balance_micros),
    CONSTRAINT fk_budget_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.embedding_reservations (
    org_id UUID NOT NULL,
    reservation_id UUID NOT NULL,
    repository_id UUID NOT NULL,
    principal_id UUID NOT NULL,
    provider VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    estimated_tokens INTEGER NOT NULL,
    estimated_micros BIGINT NOT NULL,
    settled_micros BIGINT DEFAULT NULL,
    state VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL,
    failure_reason TEXT DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, reservation_id),
    CONSTRAINT uq_reservation_idempotency UNIQUE (org_id, idempotency_key),
    CONSTRAINT ck_reservation_state CHECK (state IN ('reserved', 'committed', 'released', 'uncertain_hold')),
    CONSTRAINT ck_reservation_tokens_pos CHECK (estimated_tokens > 0),
    CONSTRAINT ck_reservation_micros_pos CHECK (estimated_micros > 0),
    CONSTRAINT fk_reservation_member FOREIGN KEY (org_id, principal_id)
        REFERENCES public.memberships (org_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT fk_reservation_repo FOREIGN KEY (org_id, repository_id)
        REFERENCES public.repositories (org_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_embedding_reservations_state ON public.embedding_reservations (
    org_id, repository_id, state
);

CREATE TABLE public.price_schedules (
    provider VARCHAR(64) NOT NULL,
    model_id VARCHAR(128) NOT NULL,
    version VARCHAR(32) NOT NULL,
    input_micros_per_1k BIGINT NOT NULL,
    effective_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (provider, model_id, version),
    CONSTRAINT ck_price_rate_pos CHECK (input_micros_per_1k > 0),
    CONSTRAINT ck_price_effective_before_expires CHECK (effective_at < expires_at)
);
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(r"""
DO $$
BEGIN
    IF EXISTS(SELECT 1 FROM public.embedding_reservations WHERE state IN ('reserved', 'committed', 'uncertain_hold')) THEN
        RAISE EXCEPTION 'active or committed embedding reservations must be preserved';
    END IF;
END $$;
DROP TABLE IF EXISTS public.price_schedules;
DROP TABLE IF EXISTS public.embedding_reservations;
DROP TABLE IF EXISTS public.embedding_budgets;
""")
