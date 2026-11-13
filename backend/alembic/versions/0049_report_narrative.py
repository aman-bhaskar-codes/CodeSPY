# ruff: noqa: E501
"""Report narrative: a synthetic investigation pinned to one stored architecture report.

Widens the investigation mode and target, pins a spec to a report (id and digest), lets an evidence row
describe a report statement instead of a source range (a strict per-type CHECK keeps rows either fully
source-bound or fully report-bound), and records which narrative describes which report in an immutable,
guarded link table. Existing rows, hashes and constraints keep their meaning.
"""

from alembic import op

revision = "0049_report_narrative"
down_revision = "0048_architecture_reports"
branch_labels = None
depends_on = None

UP = r"""
SET LOCAL lock_timeout='5s';

ALTER TABLE public.investigations DROP CONSTRAINT ck_investigation_mode;
ALTER TABLE public.investigations ADD CONSTRAINT ck_investigation_mode
 CHECK (mode IN ('short_path','two_specialist','report_narrative'));
ALTER TABLE public.investigations DROP CONSTRAINT ck_investigation_target_type;
ALTER TABLE public.investigations ADD CONSTRAINT ck_investigation_target_type
 CHECK (target_type IN ('finding','repository','commit','diff','architecture_report'));

ALTER TABLE public.investigation_specs DROP CONSTRAINT investigation_specs_mode_check;
ALTER TABLE public.investigation_specs ADD CONSTRAINT investigation_specs_mode_check
 CHECK (mode IN ('short_path','two_specialist','report_narrative'));
ALTER TABLE public.investigation_specs DROP CONSTRAINT investigation_specs_target_type_check;
ALTER TABLE public.investigation_specs ADD CONSTRAINT investigation_specs_target_type_check
 CHECK (target_type IN ('repository','finding','architecture_report'));
ALTER TABLE public.investigation_specs
 ADD COLUMN architecture_report_id uuid,
 ADD COLUMN architecture_report_digest text;
ALTER TABLE public.investigation_specs
 ADD CONSTRAINT fk_investigation_spec_report FOREIGN KEY(org_id,architecture_report_id)
  REFERENCES public.architecture_reports(org_id,id),
 ADD CONSTRAINT ck_investigation_spec_report_pin CHECK (
  (architecture_report_id IS NULL) = (architecture_report_digest IS NULL)
  AND (architecture_report_id IS NOT NULL) = (mode='report_narrative')
  AND (architecture_report_id IS NOT NULL) = (target_type='architecture_report')
  AND (architecture_report_digest IS NULL OR architecture_report_digest ~ '^[0-9a-f]{64}$'));

ALTER TABLE public.investigation_evidence
 ALTER COLUMN source_path DROP NOT NULL,
 ALTER COLUMN file_sha256 DROP NOT NULL,
 ALTER COLUMN original_start_line DROP NOT NULL,
 ALTER COLUMN original_end_line DROP NOT NULL,
 ALTER COLUMN safe_start_line DROP NOT NULL,
 ALTER COLUMN safe_end_line DROP NOT NULL,
 ADD COLUMN fact_ref text,
 ADD CONSTRAINT ck_evidence_fact_ref_bytes CHECK (fact_ref IS NULL OR octet_length(fact_ref) BETWEEN 1 AND 256),
 ADD CONSTRAINT ck_evidence_source_or_fact CHECK (
  (observation_type='report_fact' AND fact_ref IS NOT NULL AND source_path IS NULL AND file_sha256 IS NULL
    AND original_start_line IS NULL AND original_end_line IS NULL AND safe_start_line IS NULL AND safe_end_line IS NULL)
  OR (observation_type<>'report_fact' AND fact_ref IS NULL AND source_path IS NOT NULL AND file_sha256 IS NOT NULL
    AND original_start_line IS NOT NULL AND original_end_line IS NOT NULL
    AND safe_start_line IS NOT NULL AND safe_end_line IS NOT NULL));

CREATE TABLE public.architecture_report_narratives (
 org_id uuid NOT NULL,id uuid NOT NULL,
 architecture_report_id uuid NOT NULL,repository_id uuid NOT NULL,snapshot_id uuid NOT NULL,
 investigation_id uuid NOT NULL,investigation_report_id uuid NOT NULL,
 spec_hash text NOT NULL,report_digest text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(org_id,id),
 UNIQUE(org_id,architecture_report_id,spec_hash),
 UNIQUE(org_id,investigation_report_id),
 FOREIGN KEY(org_id,architecture_report_id) REFERENCES public.architecture_reports(org_id,id),
 FOREIGN KEY(org_id,investigation_id) REFERENCES public.investigations(org_id,id),
 FOREIGN KEY(org_id,investigation_report_id) REFERENCES public.investigation_reports(org_id,id),
 CHECK(spec_hash ~ '^[0-9a-f]{64}$' AND report_digest ~ '^[0-9a-f]{64}$')
);
CREATE INDEX ix_report_narratives_report ON public.architecture_report_narratives(org_id,architecture_report_id,created_at,id);
CREATE TRIGGER guard_report_narrative_immutable BEFORE UPDATE OR DELETE ON public.architecture_report_narratives
 FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
CREATE FUNCTION public.codespy_report_narrative_guard() RETURNS trigger LANGUAGE plpgsql SECURITY INVOKER AS $$
DECLARE ok integer;
BEGIN
 SELECT count(*) INTO ok
 FROM public.investigation_specs s
 JOIN public.architecture_reports r ON r.org_id=s.org_id AND r.id=s.architecture_report_id
 JOIN public.investigation_reports ir ON ir.org_id=s.org_id AND ir.spec_hash=s.spec_hash
 WHERE s.org_id=NEW.org_id AND s.spec_hash=NEW.spec_hash AND s.mode='report_narrative'
   AND s.architecture_report_id=NEW.architecture_report_id AND s.architecture_report_digest=NEW.report_digest
   AND r.report_digest=NEW.report_digest
   AND r.repository_id=NEW.repository_id AND r.snapshot_id=NEW.snapshot_id
   AND s.repository_id=NEW.repository_id AND s.snapshot_id=NEW.snapshot_id
   AND ir.id=NEW.investigation_report_id AND ir.investigation_id=NEW.investigation_id;
 IF ok<>1 THEN
  RAISE EXCEPTION 'report narrative differs from its report, spec or published investigation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER guard_report_narrative BEFORE INSERT ON public.architecture_report_narratives
 FOR EACH ROW EXECUTE FUNCTION public.codespy_report_narrative_guard();
REVOKE ALL ON FUNCTION public.codespy_report_narrative_guard() FROM PUBLIC;

DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_api') THEN
  GRANT SELECT ON public.architecture_reports, public.architecture_report_narratives TO p2_api;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_broker') THEN
  GRANT SELECT ON public.architecture_reports, public.architecture_report_narratives TO p2_broker;
  GRANT INSERT ON public.architecture_report_narratives TO p2_broker;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='p2_operator') THEN
  GRANT SELECT ON public.architecture_report_narratives TO p2_operator;
 END IF;
 IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='codespy_app') THEN
  GRANT SELECT, INSERT ON public.architecture_report_narratives TO codespy_app;
 END IF;
END $$;
"""

DOWN = r"""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.architecture_report_narratives)
  OR EXISTS(SELECT 1 FROM public.investigation_specs WHERE mode='report_narrative' OR target_type='architecture_report')
  OR EXISTS(SELECT 1 FROM public.investigations WHERE mode='report_narrative' OR target_type='architecture_report')
  OR EXISTS(SELECT 1 FROM public.investigation_evidence WHERE observation_type='report_fact') THEN
  RAISE EXCEPTION 'report narrative history must be preserved' USING ERRCODE='23514'; END IF;
END $$;
DROP TABLE public.architecture_report_narratives;
DROP FUNCTION public.codespy_report_narrative_guard();
ALTER TABLE public.investigation_evidence
 DROP CONSTRAINT ck_evidence_source_or_fact,
 DROP CONSTRAINT ck_evidence_fact_ref_bytes,
 DROP COLUMN fact_ref,
 ALTER COLUMN source_path SET NOT NULL,
 ALTER COLUMN file_sha256 SET NOT NULL,
 ALTER COLUMN original_start_line SET NOT NULL,
 ALTER COLUMN original_end_line SET NOT NULL,
 ALTER COLUMN safe_start_line SET NOT NULL,
 ALTER COLUMN safe_end_line SET NOT NULL;
ALTER TABLE public.investigation_specs
 DROP CONSTRAINT ck_investigation_spec_report_pin,
 DROP CONSTRAINT fk_investigation_spec_report,
 DROP COLUMN architecture_report_digest,
 DROP COLUMN architecture_report_id;
ALTER TABLE public.investigation_specs DROP CONSTRAINT investigation_specs_target_type_check;
ALTER TABLE public.investigation_specs ADD CONSTRAINT investigation_specs_target_type_check
 CHECK (target_type IN ('repository','finding'));
ALTER TABLE public.investigation_specs DROP CONSTRAINT investigation_specs_mode_check;
ALTER TABLE public.investigation_specs ADD CONSTRAINT investigation_specs_mode_check
 CHECK (mode IN ('short_path','two_specialist'));
ALTER TABLE public.investigations DROP CONSTRAINT ck_investigation_target_type;
ALTER TABLE public.investigations ADD CONSTRAINT ck_investigation_target_type
 CHECK (target_type IN ('finding','repository','commit','diff'));
ALTER TABLE public.investigations DROP CONSTRAINT ck_investigation_mode;
ALTER TABLE public.investigations ADD CONSTRAINT ck_investigation_mode
 CHECK (mode IN ('short_path','two_specialist'));
"""


def upgrade() -> None:
    op.execute(UP)


def downgrade() -> None:
    op.execute(DOWN)
