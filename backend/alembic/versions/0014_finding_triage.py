# ruff: noqa: E501
"""Durable human triage event history and optimistic projections."""

from alembic import op

revision = "0014_finding_triage"
down_revision = "0013_scan_authority"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.finding_triage_events (
    org_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    run_id UUID NOT NULL,
    finding_id UUID NOT NULL,
    event_id UUID NOT NULL,
    principal_id UUID NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    PRIMARY KEY (org_id, snapshot_id, run_id, finding_id, event_id),
    CONSTRAINT fk_triage_event_finding FOREIGN KEY (org_id, snapshot_id, run_id, finding_id)
        REFERENCES public.finding_occurrences (org_id, snapshot_id, run_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_triage_event_member FOREIGN KEY (org_id, principal_id)
        REFERENCES public.memberships (org_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT ck_triage_event_state CHECK (
        state IN ('unreviewed', 'confirmed', 'false_positive', 'mitigated', 'accepted_risk')
    ),
    CONSTRAINT ck_triage_event_reason CHECK (
        reason IN ('none', 'code_refactor', 'compensating_control', 'business_context', 'test_code', 'vendor_dependency')
    ),
    CONSTRAINT ck_triage_event_reason_required CHECK (
        (state IN ('false_positive', 'mitigated', 'accepted_risk') AND reason <> 'none')
        OR (state IN ('unreviewed', 'confirmed'))
    )
);

CREATE INDEX ix_triage_events_history ON public.finding_triage_events (
    org_id, snapshot_id, run_id, finding_id, created_at, event_id
);

CREATE FUNCTION public.codespy_triage_event_immutable_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
    RAISE EXCEPTION 'triage event history is immutable' USING ERRCODE='23514';
END $$;

CREATE TRIGGER guard_triage_event_immutable
    BEFORE UPDATE OR DELETE ON public.finding_triage_events
    FOR EACH ROW EXECUTE FUNCTION public.codespy_triage_event_immutable_guard();

CREATE TABLE public.finding_triage_projections (
    org_id UUID NOT NULL,
    snapshot_id UUID NOT NULL,
    run_id UUID NOT NULL,
    finding_id UUID NOT NULL,
    current_state TEXT NOT NULL,
    current_reason TEXT NOT NULL,
    last_event_id UUID NOT NULL,
    updated_by UUID NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (org_id, snapshot_id, run_id, finding_id),
    CONSTRAINT fk_triage_proj_finding FOREIGN KEY (org_id, snapshot_id, run_id, finding_id)
        REFERENCES public.finding_occurrences (org_id, snapshot_id, run_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_triage_proj_event FOREIGN KEY (org_id, snapshot_id, run_id, finding_id, last_event_id)
        REFERENCES public.finding_triage_events (org_id, snapshot_id, run_id, finding_id, event_id) ON DELETE RESTRICT,
    CONSTRAINT fk_triage_proj_member FOREIGN KEY (org_id, updated_by)
        REFERENCES public.memberships (org_id, principal_id) ON DELETE RESTRICT,
    CONSTRAINT ck_triage_proj_state CHECK (
        current_state IN ('unreviewed', 'confirmed', 'false_positive', 'mitigated', 'accepted_risk')
    ),
    CONSTRAINT ck_triage_proj_reason CHECK (
        current_reason IN ('none', 'code_refactor', 'compensating_control', 'business_context', 'test_code', 'vendor_dependency')
    ),
    CONSTRAINT ck_triage_proj_version CHECK (version >= 1)
);

CREATE INDEX ix_triage_proj_state ON public.finding_triage_projections (
    org_id, snapshot_id, run_id, current_state, finding_id
);
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(r"""
    DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.finding_triage_events) THEN
            RAISE EXCEPTION 'triage history must be preserved';
        END IF;
    END $$;
    DROP TRIGGER IF EXISTS guard_triage_event_immutable ON public.finding_triage_events;
    DROP FUNCTION IF EXISTS public.codespy_triage_event_immutable_guard();
    DROP TABLE IF EXISTS public.finding_triage_projections;
    DROP TABLE IF EXISTS public.finding_triage_events;
    """)
