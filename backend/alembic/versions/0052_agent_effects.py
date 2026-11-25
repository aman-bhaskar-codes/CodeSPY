# ruff: noqa: E501
"""Agent effect journal and session control.

The journal records what the agent loop decided and did, as encrypted, append-only rows: a model
turn's answer, and for each tool call its plan (intent), the moment it began (started), and its
result (receipt). A restarted loop reads them to replay model calls and finished tools, to apply an
approved plan, and to recognise a command that began but never reported, which is never re-run.
Sessions gain a lease (one loop per session) and a control flag (interrupt or cancel).
"""

from alembic import op

revision = "0052_agent_effects"
down_revision = "0051_workspaces"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

ALTER TABLE public.agent_sessions
 ADD COLUMN lease_owner text,
 ADD COLUMN lease_until timestamptz,
 ADD COLUMN control text,
 ADD CONSTRAINT ck_agent_session_control CHECK(control IS NULL OR control IN ('interrupt','cancel')),
 ADD CONSTRAINT ck_agent_session_lease CHECK((lease_owner IS NULL)=(lease_until IS NULL)),
 ADD CONSTRAINT ck_agent_session_lease_owner CHECK(lease_owner IS NULL OR octet_length(lease_owner) BETWEEN 1 AND 128);

CREATE TABLE public.agent_effects (
 org_id uuid NOT NULL,session_id uuid NOT NULL,effect_id text NOT NULL,
 phase text NOT NULL,kind text NOT NULL,
 payload_ciphertext bytea NOT NULL,payload_bytes integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,session_id,effect_id,phase),
 CONSTRAINT fk_agent_effect_session FOREIGN KEY(org_id,session_id) REFERENCES public.agent_sessions(org_id,id),
 CHECK(effect_id ~ '^[A-Za-z0-9_.:-]{1,160}$'),
 CHECK(phase IN ('intent','started','receipt')),
 CHECK(kind IN ('model','tool')),
 CHECK(payload_bytes BETWEEN 2 AND 2097152),
 CHECK(octet_length(payload_ciphertext)=payload_bytes+28)
);
CREATE TRIGGER guard_agent_effect_immutable BEFORE UPDATE OR DELETE ON public.agent_effects
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT UPDATE(control) ON public.agent_sessions TO p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT UPDATE(lease_owner,lease_until,control) ON public.agent_sessions TO p2_broker;
  GRANT SELECT,INSERT ON public.agent_effects TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
  GRANT UPDATE(lease_owner,lease_until,control) ON public.agent_sessions TO codespy_app;
  GRANT SELECT,INSERT ON public.agent_effects TO codespy_app;
 END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.agent_effects) THEN
  RAISE EXCEPTION 'agent effect history must be preserved' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.agent_effects;
ALTER TABLE public.agent_sessions
 DROP CONSTRAINT ck_agent_session_lease_owner,
 DROP CONSTRAINT ck_agent_session_lease,
 DROP CONSTRAINT ck_agent_session_control,
 DROP COLUMN control,DROP COLUMN lease_until,DROP COLUMN lease_owner;
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
