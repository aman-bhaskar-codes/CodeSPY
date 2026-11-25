# ruff: noqa: E501
"""Agent sessions and their append-only, gap-free event log.

``agent_sessions.last_seq`` is the allocator: a writer increments it by one (row lock) and inserts the
event with that sequence in the same transaction. Guards make the invariants database facts, not
conventions: an event's ``seq`` must equal the session's ``last_seq``; ``last_seq`` moves by at most one
per update; and a deferred check at commit rejects any transaction that leaves a gap. Every insert
notifies listeners on channel ``codespy_agent_events`` so every API process can wake its streams.
Events are never updated or deleted; checkpoints append with an expected predecessor; an approval
decides once.
"""

from alembic import op

revision = "0050_agent_sessions"
down_revision = "0049_report_narrative"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

CREATE TABLE public.agent_sessions (
 org_id uuid NOT NULL,id uuid NOT NULL,
 repository_id uuid NOT NULL,base_snapshot_id uuid NOT NULL,principal_id uuid NOT NULL,
 mode text NOT NULL,status text NOT NULL DEFAULT 'idle',model_profile text NOT NULL,
 limits_json jsonb NOT NULL,last_seq bigint NOT NULL DEFAULT 0,
 idempotency_key text,request_digest text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 FOREIGN KEY(org_id,repository_id,base_snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 FOREIGN KEY(org_id,principal_id) REFERENCES public.memberships(org_id,principal_id),
 CONSTRAINT uq_agent_session_idempotency UNIQUE(org_id,principal_id,idempotency_key),
 CONSTRAINT ck_agent_session_mode CHECK(mode IN ('plan','act')),
 CONSTRAINT ck_agent_session_status CHECK(status IN ('idle','running','waiting_approval','interrupted','completed','failed','cancelled')),
 CONSTRAINT ck_agent_session_profile CHECK(model_profile IN ('agent','fast','deep')),
 CHECK(last_seq>=0),
 CHECK(jsonb_typeof(limits_json)='object'),
 CHECK((idempotency_key IS NULL)=(request_digest IS NULL)),
 CHECK(idempotency_key IS NULL OR octet_length(idempotency_key) BETWEEN 1 AND 128),
 CHECK(request_digest IS NULL OR request_digest ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_agent_sessions_repo ON public.agent_sessions(org_id,repository_id,created_at DESC,id);
CREATE INDEX ix_agent_sessions_principal ON public.agent_sessions(org_id,principal_id,created_at DESC,id);

CREATE FUNCTION public.codespy_agent_session_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.last_seq<>0 THEN RAISE EXCEPTION 'a new agent session starts at sequence 0' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 END IF;
 IF NEW.org_id<>OLD.org_id OR NEW.id<>OLD.id OR NEW.repository_id<>OLD.repository_id
    OR NEW.base_snapshot_id<>OLD.base_snapshot_id OR NEW.principal_id<>OLD.principal_id
    OR NEW.created_at<>OLD.created_at
    OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
    OR NEW.request_digest IS DISTINCT FROM OLD.request_digest THEN
  RAISE EXCEPTION 'agent session identity is immutable' USING ERRCODE='23514'; END IF;
 IF NEW.last_seq<OLD.last_seq OR NEW.last_seq>OLD.last_seq+1 THEN
  RAISE EXCEPTION 'agent event sequence advances by one' USING ERRCODE='23514'; END IF;
 IF OLD.status IN ('completed','failed','cancelled') AND NEW.status<>OLD.status THEN
  RAISE EXCEPTION 'a finished agent session cannot change status' USING ERRCODE='23514'; END IF;
 NEW.updated_at:=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER guard_agent_session BEFORE INSERT OR UPDATE ON public.agent_sessions
 FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_session_guard();
CREATE TRIGGER guard_agent_session_no_delete BEFORE DELETE ON public.agent_sessions
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE TABLE public.agent_events (
 org_id uuid NOT NULL,session_id uuid NOT NULL,seq bigint NOT NULL,
 type text NOT NULL,turn integer NOT NULL DEFAULT 0,parent text,event_key text,
 payload_ciphertext bytea NOT NULL,payload_bytes integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,session_id,seq),
 CONSTRAINT fk_agent_event_session FOREIGN KEY(org_id,session_id) REFERENCES public.agent_sessions(org_id,id),
 CONSTRAINT uq_agent_event_key UNIQUE(org_id,session_id,event_key),
 CHECK(seq>=1),
 CHECK(type ~ '^[a-z]+(\.[a-z]+)*$' AND octet_length(type)<=64 AND type<>'stream.reset'),
 CHECK(turn>=0),
 CHECK(parent IS NULL OR octet_length(parent) BETWEEN 1 AND 160),
 CHECK(event_key IS NULL OR octet_length(event_key) BETWEEN 1 AND 128),
 CHECK(payload_bytes BETWEEN 2 AND 16384),
 CHECK(octet_length(payload_ciphertext)=payload_bytes+28)
);
CREATE INDEX ix_agent_events_type ON public.agent_events(org_id,session_id,type,seq);

CREATE FUNCTION public.codespy_agent_event_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE cur bigint;
BEGIN
 SELECT last_seq INTO cur FROM public.agent_sessions WHERE org_id=NEW.org_id AND id=NEW.session_id;
 IF cur IS DISTINCT FROM NEW.seq THEN
  RAISE EXCEPTION 'agent event sequence must equal the session sequence' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_agent_event BEFORE INSERT ON public.agent_events
 FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_event_guard();
CREATE TRIGGER guard_agent_event_immutable BEFORE UPDATE OR DELETE ON public.agent_events
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE FUNCTION public.codespy_agent_event_notify() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 PERFORM pg_notify('codespy_agent_events', NEW.session_id::text||':'||NEW.seq::text);
 RETURN NULL;
END $$;
CREATE TRIGGER notify_agent_event AFTER INSERT ON public.agent_events
 FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_event_notify();

CREATE FUNCTION public.codespy_agent_seq_check() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE cur bigint; top bigint;
BEGIN
 SELECT last_seq INTO cur FROM public.agent_sessions WHERE org_id=NEW.org_id AND id=NEW.id;
 SELECT coalesce(max(seq),0) INTO top FROM public.agent_events WHERE org_id=NEW.org_id AND session_id=NEW.id;
 IF cur IS DISTINCT FROM top THEN
  RAISE EXCEPTION 'agent event sequence has a gap' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER agent_seq_check AFTER UPDATE ON public.agent_sessions
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_seq_check();

CREATE TABLE public.agent_session_checkpoints (
 org_id uuid NOT NULL,session_id uuid NOT NULL,sequence integer NOT NULL,
 state_digest text NOT NULL,state_ciphertext bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,session_id,sequence),
 CONSTRAINT fk_agent_checkpoint_session FOREIGN KEY(org_id,session_id) REFERENCES public.agent_sessions(org_id,id),
 CHECK(sequence>=1),
 CHECK(state_digest ~ '^[0-9a-f]{64}$'),
 CHECK(octet_length(state_ciphertext) BETWEEN 29 AND 1048576)
);
CREATE FUNCTION public.codespy_agent_checkpoint_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE top integer;
BEGIN
 SELECT coalesce(max(sequence),0) INTO top FROM public.agent_session_checkpoints
  WHERE org_id=NEW.org_id AND session_id=NEW.session_id;
 IF NEW.sequence<>top+1 THEN
  RAISE EXCEPTION 'agent checkpoint must follow its predecessor' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_agent_checkpoint BEFORE INSERT ON public.agent_session_checkpoints
 FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_checkpoint_guard();
CREATE TRIGGER guard_agent_checkpoint_immutable BEFORE UPDATE OR DELETE ON public.agent_session_checkpoints
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

CREATE TABLE public.agent_approvals (
 org_id uuid NOT NULL,session_id uuid NOT NULL,approval_id text NOT NULL,
 subject_kind text NOT NULL,subject_ref text NOT NULL,
 status text NOT NULL DEFAULT 'pending',reason text NOT NULL DEFAULT '',rule_json jsonb,
 requested_seq bigint NOT NULL,decided_by uuid,decided_at timestamptz,expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,session_id,approval_id),
 CONSTRAINT fk_agent_approval_session FOREIGN KEY(org_id,session_id) REFERENCES public.agent_sessions(org_id,id),
 CHECK(octet_length(approval_id) BETWEEN 1 AND 128),
 CHECK(subject_kind IN ('tool_call','patch','plan','publish')),
 CHECK(octet_length(subject_ref) BETWEEN 1 AND 128),
 CHECK(status IN ('pending','approved','rejected','expired')),
 CHECK(rule_json IS NULL OR jsonb_typeof(rule_json)='object'),
 CHECK((status='pending')=(decided_at IS NULL))
);
CREATE FUNCTION public.codespy_agent_approval_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
BEGIN
 IF NEW.org_id<>OLD.org_id OR NEW.session_id<>OLD.session_id OR NEW.approval_id<>OLD.approval_id
    OR NEW.subject_kind<>OLD.subject_kind OR NEW.subject_ref<>OLD.subject_ref
    OR NEW.requested_seq<>OLD.requested_seq OR NEW.expires_at<>OLD.expires_at THEN
  RAISE EXCEPTION 'agent approval subject is immutable' USING ERRCODE='23514'; END IF;
 IF OLD.status<>'pending' THEN
  RAISE EXCEPTION 'an agent approval is decided once' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_agent_approval BEFORE UPDATE ON public.agent_approvals
 FOR EACH ROW EXECUTE FUNCTION public.codespy_agent_approval_guard();
CREATE TRIGGER guard_agent_approval_no_delete BEFORE DELETE ON public.agent_approvals
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();

REVOKE ALL ON FUNCTION public.codespy_agent_session_guard(), public.codespy_agent_event_guard(),
 public.codespy_agent_event_notify(), public.codespy_agent_seq_check(),
 public.codespy_agent_checkpoint_guard(), public.codespy_agent_approval_guard() FROM PUBLIC;

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT SELECT,INSERT ON public.agent_sessions,public.agent_events,public.agent_approvals TO p2_api;
  GRANT UPDATE(status,mode,limits_json,last_seq,updated_at) ON public.agent_sessions TO p2_api;
  GRANT UPDATE(status,rule_json,decided_by,decided_at) ON public.agent_approvals TO p2_api;
  GRANT SELECT(org_id,session_id,sequence,state_digest,created_at) ON public.agent_session_checkpoints TO p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT SELECT,INSERT ON public.agent_sessions,public.agent_events,public.agent_approvals,public.agent_session_checkpoints TO p2_broker;
  GRANT UPDATE(status,mode,limits_json,last_seq,updated_at) ON public.agent_sessions TO p2_broker;
  GRANT UPDATE(status,rule_json,decided_by,decided_at) ON public.agent_approvals TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
  GRANT SELECT ON public.agent_sessions,public.agent_events,public.agent_approvals TO p2_operator;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
  GRANT SELECT,INSERT ON public.agent_sessions,public.agent_events,public.agent_approvals,public.agent_session_checkpoints TO codespy_app;
  GRANT UPDATE(status,mode,limits_json,last_seq,updated_at) ON public.agent_sessions TO codespy_app;
  GRANT UPDATE(status,rule_json,decided_by,decided_at) ON public.agent_approvals TO codespy_app;
 END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.agent_sessions) THEN
  RAISE EXCEPTION 'agent session history must be preserved' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.agent_approvals;
DROP TABLE public.agent_session_checkpoints;
DROP TABLE public.agent_events;
DROP TABLE public.agent_sessions;
DROP FUNCTION public.codespy_agent_approval_guard();
DROP FUNCTION public.codespy_agent_checkpoint_guard();
DROP FUNCTION public.codespy_agent_seq_check();
DROP FUNCTION public.codespy_agent_event_notify();
DROP FUNCTION public.codespy_agent_event_guard();
DROP FUNCTION public.codespy_agent_session_guard();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
