# ruff: noqa: E501
"""Immutable architecture reports derived from published AST facts.

A report is a pure function of the fact runs it names. The insert guard requires every named run to
be a published AST-facts analysis of the same snapshot, no file in two runs, and the stored inputs
digest to equal the digest recomputed from the facts rows, so a report cannot cite facts that do
not exist or silently change its inputs. Rows are never updated or deleted.
"""

from alembic import op

revision = "0048_architecture_reports"
down_revision = "0047_recovery_snapshot_lock"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';
CREATE TABLE public.architecture_reports (
 org_id uuid NOT NULL,id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,
 principal_id uuid NOT NULL,schema_version integer NOT NULL,
 inputs_digest text NOT NULL,report_digest text NOT NULL,
 fact_runs jsonb NOT NULL,report_json jsonb NOT NULL,size_bytes integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 UNIQUE(org_id,repository_id,snapshot_id,inputs_digest),
 FOREIGN KEY(org_id,repository_id,snapshot_id) REFERENCES public.source_snapshots(org_id,repository_id,id),
 FOREIGN KEY(org_id,principal_id) REFERENCES public.memberships(org_id,principal_id),
 CHECK(schema_version=1),
 CHECK(inputs_digest ~ '^[0-9a-f]{64}$' AND report_digest ~ '^[0-9a-f]{64}$'),
 CHECK(jsonb_typeof(fact_runs)='array' AND jsonb_array_length(fact_runs) BETWEEN 1 AND 64),
 CHECK(jsonb_typeof(report_json)='object' AND size_bytes BETWEEN 2 AND 8388608
   AND octet_length(report_json::text)<=8388608+65536)
);
CREATE INDEX ix_architecture_reports_history ON public.architecture_reports(org_id,repository_id,snapshot_id,created_at,id);
CREATE TRIGGER guard_architecture_report_immutable BEFORE UPDATE OR DELETE ON public.architecture_reports
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
CREATE FUNCTION public.codespy_architecture_report_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE n integer; total integer; files integer; distinct_files integer; recomputed text;
BEGIN
 SELECT count(*) INTO n FROM jsonb_array_elements_text(NEW.fact_runs) r
  JOIN public.analysis_runs a ON a.org_id=NEW.org_id AND a.snapshot_id=NEW.snapshot_id AND a.id=r::uuid
   AND a.status IN ('completed','partial') AND (a.config->'adapters') ? 'ast_facts';
 SELECT count(DISTINCT r) INTO total FROM jsonb_array_elements_text(NEW.fact_runs) r;
 IF n<>jsonb_array_length(NEW.fact_runs) OR total<>jsonb_array_length(NEW.fact_runs) THEN
  RAISE EXCEPTION 'architecture report input differs' USING ERRCODE='23514'; END IF;
 SELECT count(*),count(DISTINCT f.file_id) INTO files,distinct_files FROM public.ast_file_facts f
  WHERE f.org_id=NEW.org_id AND f.snapshot_id=NEW.snapshot_id
  AND f.run_id IN (SELECT r::uuid FROM jsonb_array_elements_text(NEW.fact_runs) r);
 IF files=0 OR files<>distinct_files THEN
  RAISE EXCEPTION 'architecture report facts overlap or are missing' USING ERRCODE='23514'; END IF;
 recomputed:=encode(sha256(convert_to(
  (SELECT string_agg(r,E'\n' ORDER BY r COLLATE "C") FROM jsonb_array_elements_text(NEW.fact_runs) r)||E'\n'||
  (SELECT string_agg(f.facts_sha256,E'\n' ORDER BY f.facts_sha256 COLLATE "C") FROM public.ast_file_facts f
    WHERE f.org_id=NEW.org_id AND f.snapshot_id=NEW.snapshot_id
    AND f.run_id IN (SELECT r::uuid FROM jsonb_array_elements_text(NEW.fact_runs) r)),'UTF8')),'hex');
 IF recomputed<>NEW.inputs_digest OR NEW.report_json->>'inputs_digest' IS DISTINCT FROM NEW.inputs_digest
  OR NEW.report_json->>'org_id' IS DISTINCT FROM NEW.org_id::text
  OR NEW.report_json->>'repository_id' IS DISTINCT FROM NEW.repository_id::text
  OR NEW.report_json->>'snapshot_id' IS DISTINCT FROM NEW.snapshot_id::text
  OR (NEW.report_json->>'schema_version')::integer IS DISTINCT FROM NEW.schema_version
  OR NEW.report_json->'coverage'->'fact_runs' IS DISTINCT FROM NEW.fact_runs
 THEN RAISE EXCEPTION 'architecture report identity differs' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_architecture_report BEFORE INSERT ON public.architecture_reports
 FOR EACH ROW EXECUTE FUNCTION public.codespy_architecture_report_guard();
REVOKE ALL ON FUNCTION public.codespy_architecture_report_guard() FROM PUBLIC;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.architecture_reports) THEN
  RAISE EXCEPTION 'architecture report history must be preserved' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.architecture_reports;
DROP FUNCTION public.codespy_architecture_report_guard();
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
