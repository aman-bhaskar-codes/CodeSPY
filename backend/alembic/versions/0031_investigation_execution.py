# ruff: noqa: E501
"""Durable scoped investigations, execution checkpoints, and immutable reports."""

from alembic import op

revision = "0031_investigation_execution"
down_revision = "0030_model_runtime_authority"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL lock_timeout='5s';

-- ---------------------------------------------------------------------------
-- 1. Extend jobs table for investigate_snapshot operation
-- ---------------------------------------------------------------------------

ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK (
  operation IN (
    'admit_source',
    'inventory_snapshot',
    'analyze_structure',
    'scan_snapshot',
    'index_snapshot',
    'investigate_snapshot'
  )
);

ALTER TABLE public.jobs
  ADD COLUMN investigation_id uuid,
  ADD COLUMN spec_hash text,
  ADD COLUMN result_report_id uuid;

ALTER TABLE public.jobs ADD CONSTRAINT ck_job_investigation CHECK (
  (operation = 'investigate_snapshot' AND investigation_id IS NOT NULL AND spec_hash IS NOT NULL AND submission_id IS NULL AND result_run_id IS NULL AND result_object_id IS NULL AND index_spec_id IS NULL AND result_index_id IS NULL)
  OR (operation <> 'investigate_snapshot' AND investigation_id IS NULL AND spec_hash IS NULL AND result_report_id IS NULL)
);

-- ---------------------------------------------------------------------------
-- 2. Extend investigations table for execution/job bindings
-- ---------------------------------------------------------------------------

ALTER TABLE public.investigations
  ADD COLUMN spec_hash text,
  ADD COLUMN snapshot_id uuid,
  ADD COLUMN current_job_id uuid,
  ADD COLUMN deadline timestamptz,
  ADD COLUMN next_event_sequence bigint NOT NULL DEFAULT 1,
  ADD COLUMN final_report_id uuid,
  ADD COLUMN execution_revision integer NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- 3. Investigation Specs (immutable canonical specifications)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_specs (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  spec_hash text NOT NULL,
  repository_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  principal_id uuid NOT NULL,
  target_type text NOT NULL,
  finding_id uuid,
  finding_run_id uuid,
  chunk_set_id uuid,
  chunk_config_id uuid,
  materialization_id uuid,
  query text NOT NULL,
  mode text NOT NULL,
  depth text NOT NULL,
  limits_json jsonb NOT NULL,
  disclosure_policy_digest text NOT NULL,
  tool_policy_revision text NOT NULL,
  model_route_revision text NOT NULL,
  model_profile_revision text NOT NULL,
  runtime_revision integer NOT NULL DEFAULT 1,
  privacy_policy_revision text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, spec_hash),
  FOREIGN KEY (org_id, repository_id, snapshot_id) REFERENCES public.source_snapshots (org_id, repository_id, id),
  FOREIGN KEY (org_id, principal_id) REFERENCES public.memberships (org_id, principal_id),
  CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  CHECK (disclosure_policy_digest ~ '^[0-9a-f]{64}$'),
  CHECK (target_type IN ('repository', 'finding')),
  CHECK (mode IN ('short_path', 'two_specialist')),
  CHECK (depth IN ('quick', 'standard', 'deep')),
  CHECK (runtime_revision >= 1),
  CHECK (octet_length(query) <= 16384)
);

-- ---------------------------------------------------------------------------
-- 4. Investigation Reports (immutable final typed reports)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_reports (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  spec_hash text NOT NULL,
  execution_revision integer NOT NULL DEFAULT 1,
  job_id uuid NOT NULL,
  verdict text NOT NULL,
  verdict_confidence double precision NOT NULL,
  summary text NOT NULL,
  report_json jsonb NOT NULL,
  report_digest text NOT NULL,
  claims_count integer NOT NULL,
  evidence_refs_count integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, job_id),
  FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  CHECK (report_digest ~ '^[0-9a-f]{64}$'),
  CHECK (claims_count >= 0),
  CHECK (evidence_refs_count >= 0),
  CHECK (execution_revision >= 1)
);

-- ---------------------------------------------------------------------------
-- 5. Investigation Publication Receipts (atomic finalization receipts)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_publication_receipts (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  spec_hash text NOT NULL,
  job_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  fencing_token bigint NOT NULL,
  report_id uuid NOT NULL,
  report_digest text NOT NULL,
  draft_digest text NOT NULL,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, job_id),
  FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  FOREIGN KEY (org_id, report_id) REFERENCES public.investigation_reports (org_id, id),
  CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  CHECK (report_digest ~ '^[0-9a-f]{64}$'),
  CHECK (draft_digest ~ '^[0-9a-f]{64}$'),
  CHECK (fencing_token >= 1)
);

-- Foreign keys on jobs and investigations now that target tables exist
ALTER TABLE public.jobs
  ADD CONSTRAINT fk_job_investigation FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  ADD CONSTRAINT fk_job_result_report FOREIGN KEY (org_id, result_report_id) REFERENCES public.investigation_reports (org_id, id);

ALTER TABLE public.investigations
  ADD CONSTRAINT fk_investigation_current_job FOREIGN KEY (org_id, current_job_id) REFERENCES public.jobs (org_id, id),
  ADD CONSTRAINT fk_investigation_final_report FOREIGN KEY (org_id, final_report_id) REFERENCES public.investigation_reports (org_id, id);

-- ---------------------------------------------------------------------------
-- 6. Investigation Execution Checkpoints (versioned job checkpoints)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_execution_checkpoints (
  org_id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  execution_revision integer NOT NULL DEFAULT 1,
  checkpoint_sequence bigint NOT NULL,
  spec_hash text NOT NULL,
  node_id uuid NOT NULL,
  node_kind text NOT NULL,
  role text NOT NULL,
  logical_ordinal integer NOT NULL,
  state_snapshot_json jsonb NOT NULL,
  state_digest text NOT NULL,
  writer_job_id uuid NOT NULL,
  writer_attempt_id uuid NOT NULL,
  writer_fencing_token bigint NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, investigation_id, execution_revision, checkpoint_sequence),
  FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  CHECK (checkpoint_sequence >= 0),
  CHECK (execution_revision >= 1),
  CHECK (writer_fencing_token >= 1),
  CHECK (spec_hash ~ '^[0-9a-f]{64}$'),
  CHECK (state_digest ~ '^[0-9a-f]{64}$'),
  CHECK (octet_length(state_snapshot_json::text) <= 524288)
);

-- ---------------------------------------------------------------------------
-- 7. Investigation Step Receipts (per-node receipts)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_step_receipts (
  org_id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  execution_revision integer NOT NULL DEFAULT 1,
  node_id uuid NOT NULL,
  node_kind text NOT NULL,
  role text NOT NULL,
  logical_ordinal integer NOT NULL,
  input_digest text NOT NULL,
  state text NOT NULL,
  result_digest text,
  call_ref text,
  reservation_id uuid,
  model_result_id uuid,
  attempt_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, investigation_id, execution_revision, node_id),
  FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  CHECK (state IN ('started', 'completed', 'failed', 'uncertain')),
  CHECK (execution_revision >= 1),
  CHECK (input_digest ~ '^[0-9a-f]{64}$')
);

-- ---------------------------------------------------------------------------
-- 8. Model Call Results (encrypted full outcome store)
-- ---------------------------------------------------------------------------

CREATE TABLE public.model_call_results (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  reservation_id uuid NOT NULL,
  logical_call_id uuid NOT NULL,
  request_digest text NOT NULL,
  model_id text NOT NULL,
  provider text NOT NULL,
  profile_revision text NOT NULL,
  outcome_status text NOT NULL,
  ciphertext bytea,
  ciphertext_digest text,
  failure_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, reservation_id),
  FOREIGN KEY (org_id, reservation_id) REFERENCES public.model_reservations (org_id, reservation_id),
  CHECK (outcome_status IN ('usable', 'invalid', 'unusable')),
  CHECK (request_digest ~ '^[0-9a-f]{64}$'),
  CHECK (ciphertext IS NULL OR octet_length(ciphertext) <= 524316)
);

-- ---------------------------------------------------------------------------
-- 9. Investigation Evidence (immutable source / tool observations)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_evidence (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  snapshot_id uuid NOT NULL,
  source_path text NOT NULL,
  file_sha256 text NOT NULL,
  original_start_line integer NOT NULL,
  original_end_line integer NOT NULL,
  safe_start_line integer NOT NULL,
  safe_end_line integer NOT NULL,
  excerpt_sha256 text NOT NULL,
  observation_type text NOT NULL,
  coverage_summary text,
  limitations text,
  tool_receipt_id uuid,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id),
  FOREIGN KEY (org_id, investigation_id) REFERENCES public.investigations (org_id, id),
  CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (excerpt_sha256 ~ '^[0-9a-f]{64}$'),
  CHECK (original_start_line >= 1 AND original_start_line <= original_end_line),
  CHECK (safe_start_line >= 1 AND safe_start_line <= safe_end_line)
);

-- ---------------------------------------------------------------------------
-- 10. Investigation Commands (durable idempotency receipts)
-- ---------------------------------------------------------------------------

CREATE TABLE public.investigation_commands (
  org_id uuid NOT NULL,
  id uuid NOT NULL,
  command_kind text NOT NULL,
  idempotency_key text NOT NULL,
  request_digest text NOT NULL,
  principal_id uuid NOT NULL,
  investigation_id uuid NOT NULL,
  job_id uuid,
  status_code integer NOT NULL,
  response_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (org_id, id),
  UNIQUE (org_id, command_kind, idempotency_key),
  FOREIGN KEY (org_id, principal_id) REFERENCES public.memberships (org_id, principal_id),
  CHECK (command_kind IN ('create', 'start', 'cancel', 'resume', 'retry')),
  CHECK (request_digest ~ '^[0-9a-f]{64}$'),
  CHECK (octet_length(idempotency_key) BETWEEN 1 AND 128)
);

-- ---------------------------------------------------------------------------
-- 11. Immutability Guards and Triggers
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION public.codespy_immutable_record_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'table % is append-only; update/delete prohibited', TG_TABLE_NAME
    USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER guard_investigation_specs_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_specs
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE TRIGGER guard_exec_checkpoints_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_execution_checkpoints
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE TRIGGER guard_investigation_reports_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_reports
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE TRIGGER guard_publication_receipts_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_publication_receipts
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE TRIGGER guard_investigation_evidence_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_evidence
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE TRIGGER guard_investigation_commands_immutable
  BEFORE UPDATE OR DELETE ON public.investigation_commands
  FOR EACH ROW EXECUTE FUNCTION public.codespy_immutable_record_guard();

CREATE OR REPLACE FUNCTION public.codespy_model_call_results_guard()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'model_call_results is immutable; updates prohibited'
      USING ERRCODE = '23514';
  END IF;
  IF TG_OP = 'DELETE' THEN
    IF OLD.expires_at >= clock_timestamp() THEN
      RAISE EXCEPTION 'unexpired model call result cannot be deleted'
        USING ERRCODE = '23514';
    END IF;
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER guard_model_call_results
  BEFORE UPDATE OR DELETE ON public.model_call_results
  FOR EACH ROW EXECUTE FUNCTION public.codespy_model_call_results_guard();

-- ---------------------------------------------------------------------------
-- 12. Role Grants & Least Privilege
-- ---------------------------------------------------------------------------

DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
    GRANT SELECT ON public.investigation_specs,
                    public.investigation_reports,
                    public.investigation_publication_receipts,
                    public.investigation_execution_checkpoints,
                    public.investigation_step_receipts,
                    public.investigation_evidence,
                    public.investigation_commands
      TO p2_api;
    GRANT INSERT ON public.investigation_specs,
                    public.investigation_commands
      TO p2_api;
    REVOKE INSERT, UPDATE, DELETE ON public.investigation_reports FROM p2_api;
    REVOKE INSERT, UPDATE, DELETE ON public.investigation_execution_checkpoints FROM p2_api;
    REVOKE INSERT, UPDATE, DELETE ON public.model_call_results FROM p2_api;
    REVOKE SELECT ON public.model_call_results FROM p2_api;
  END IF;

  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
    GRANT SELECT ON public.investigation_specs,
                    public.investigation_reports,
                    public.investigation_publication_receipts,
                    public.investigation_execution_checkpoints,
                    public.investigation_step_receipts,
                    public.investigation_evidence,
                    public.investigation_commands,
                    public.model_call_results
      TO p2_broker;
    GRANT INSERT ON public.investigation_execution_checkpoints,
                    public.investigation_step_receipts,
                    public.investigation_reports,
                    public.investigation_publication_receipts,
                    public.investigation_evidence,
                    public.model_call_results
      TO p2_broker;
  END IF;

  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
    GRANT SELECT ON public.investigation_specs,
                    public.investigation_reports,
                    public.investigation_publication_receipts,
                    public.investigation_execution_checkpoints,
                    public.investigation_step_receipts,
                    public.investigation_evidence,
                    public.investigation_commands
      TO p2_operator;
    GRANT DELETE ON public.model_call_results TO p2_operator;
  END IF;

  IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON
      public.investigation_specs,
      public.investigation_reports,
      public.investigation_publication_receipts,
      public.investigation_execution_checkpoints,
      public.investigation_step_receipts,
      public.investigation_evidence,
      public.investigation_commands,
      public.model_call_results
    TO codespy_app;
  END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';

DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM public.investigation_specs)
     OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='investigate_snapshot')
  THEN
    RAISE EXCEPTION 'investigation execution history must be preserved';
  END IF;
END $$;

DROP TRIGGER IF EXISTS guard_investigation_specs_immutable ON public.investigation_specs;
DROP TRIGGER IF EXISTS guard_exec_checkpoints_immutable ON public.investigation_execution_checkpoints;
DROP TRIGGER IF EXISTS guard_investigation_reports_immutable ON public.investigation_reports;
DROP TRIGGER IF EXISTS guard_publication_receipts_immutable ON public.investigation_publication_receipts;
DROP TRIGGER IF EXISTS guard_investigation_evidence_immutable ON public.investigation_evidence;
DROP TRIGGER IF EXISTS guard_investigation_commands_immutable ON public.investigation_commands;
DROP TRIGGER IF EXISTS guard_model_call_results ON public.model_call_results;

DROP FUNCTION IF EXISTS public.codespy_immutable_record_guard();
DROP FUNCTION IF EXISTS public.codespy_model_call_results_guard();

ALTER TABLE public.investigations
  DROP CONSTRAINT IF EXISTS fk_investigation_current_job,
  DROP CONSTRAINT IF EXISTS fk_investigation_final_report;

ALTER TABLE public.jobs
  DROP CONSTRAINT IF EXISTS fk_job_investigation,
  DROP CONSTRAINT IF EXISTS fk_job_result_report,
  DROP CONSTRAINT IF EXISTS ck_job_investigation;

DROP TABLE IF EXISTS public.investigation_commands;
DROP TABLE IF EXISTS public.investigation_evidence;
DROP TABLE IF EXISTS public.model_call_results;
DROP TABLE IF EXISTS public.investigation_step_receipts;
DROP TABLE IF EXISTS public.investigation_execution_checkpoints;
DROP TABLE IF EXISTS public.investigation_publication_receipts;
DROP TABLE IF EXISTS public.investigation_reports;
DROP TABLE IF EXISTS public.investigation_specs;

ALTER TABLE public.investigations
  DROP COLUMN IF EXISTS spec_hash,
  DROP COLUMN IF EXISTS snapshot_id,
  DROP COLUMN IF EXISTS current_job_id,
  DROP COLUMN IF EXISTS deadline,
  DROP COLUMN IF EXISTS next_event_sequence,
  DROP COLUMN IF EXISTS final_report_id,
  DROP COLUMN IF EXISTS execution_revision;

ALTER TABLE public.jobs
  DROP COLUMN IF EXISTS investigation_id,
  DROP COLUMN IF EXISTS spec_hash,
  DROP COLUMN IF EXISTS result_report_id;

ALTER TABLE public.jobs DROP CONSTRAINT IF EXISTS ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK (
  operation IN (
    'admit_source',
    'inventory_snapshot',
    'analyze_structure',
    'scan_snapshot',
    'index_snapshot'
  )
);
"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(DOWN)
