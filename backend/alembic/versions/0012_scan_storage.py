# ruff: noqa: E501
"""Immutable scoped offline scan storage."""

from alembic import op

revision = "0012_scan_storage"
down_revision = "0011_structural_authority"
branch_labels = None
depends_on = None

SQL = r"""
SET LOCAL search_path=public,pg_catalog; SET LOCAL lock_timeout='5s';

CREATE TABLE scan_specs (
	org_id UUID NOT NULL, 
	id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	principal_id UUID NOT NULL, 
	config_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	config_hash TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, id), 
	CONSTRAINT ck_scan_specs_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=config_json AND octet_length(canonical_text)<=65536), 
	CONSTRAINT ck_scan_specs_digest CHECK (config_hash=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT uq_scan_spec_scope UNIQUE (org_id, repository_id, snapshot_id, id, config_hash), 
	CONSTRAINT fk_scan_spec_snapshot FOREIGN KEY(org_id, repository_id, snapshot_id) REFERENCES source_snapshots (org_id, repository_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_scan_spec_member FOREIGN KEY(org_id, principal_id) REFERENCES memberships (org_id, principal_id) ON DELETE RESTRICT
)

;
CREATE INDEX ix_scan_specs_history ON scan_specs (org_id, repository_id, snapshot_id, created_at, id);

CREATE TABLE scan_attempt_specs (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	spec_id UUID NOT NULL, 
	config_hash TEXT NOT NULL, 
	job_id UUID NOT NULL, 
	attempt_id UUID NOT NULL, 
	fence BIGINT NOT NULL, 
	publication_spec_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	spec_digest TEXT NOT NULL, 
	captured_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id), 
	CONSTRAINT ck_scan_attempt_specs_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=publication_spec_json AND octet_length(canonical_text)<=98304), 
	CONSTRAINT ck_scan_attempt_specs_digest CHECK (spec_digest=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT uq_scan_attempt_job UNIQUE (org_id, job_id, attempt_id), 
	CONSTRAINT uq_scan_attempt_spec UNIQUE (org_id, snapshot_id, run_id, spec_id, config_hash), 
	CONSTRAINT fk_scan_attempt_spec FOREIGN KEY(org_id, repository_id, snapshot_id, spec_id, config_hash) REFERENCES scan_specs (org_id, repository_id, snapshot_id, id, config_hash) ON DELETE RESTRICT, 
	CONSTRAINT fk_scan_attempt_run FOREIGN KEY(org_id, snapshot_id, run_id) REFERENCES analysis_runs (org_id, snapshot_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_scan_attempt_worker FOREIGN KEY(org_id, job_id, attempt_id, fence) REFERENCES job_attempts (org_id, job_id, id, fence) ON DELETE RESTRICT, 
	CONSTRAINT ck_scan_attempt_fence CHECK (fence>0)
)

;

CREATE TABLE scan_reports (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	repository_id UUID NOT NULL, 
	spec_id UUID NOT NULL, 
	config_hash TEXT NOT NULL, 
	draft_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	draft_digest TEXT NOT NULL, 
	batch_hash TEXT NOT NULL, 
	coverage TEXT NOT NULL, 
	omitted_findings BIGINT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id), 
	CONSTRAINT ck_scan_reports_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=draft_json AND octet_length(canonical_text)<=4194304), 
	CONSTRAINT ck_scan_reports_digest CHECK (draft_digest=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT fk_scan_report_attempt FOREIGN KEY(org_id, snapshot_id, run_id, spec_id, config_hash) REFERENCES scan_attempt_specs (org_id, snapshot_id, run_id, spec_id, config_hash) ON DELETE RESTRICT, 
	CONSTRAINT ck_scan_report_coverage CHECK (coverage IN ('complete','partial','unknown')), 
	CONSTRAINT ck_scan_report_omissions CHECK (omitted_findings BETWEEN 0 AND 502000), 
	CONSTRAINT ck_scan_report_batch CHECK (batch_hash ~ '^[0-9a-f]{64}$')
)

;
CREATE INDEX ix_scan_reports_history ON scan_reports (org_id, repository_id, snapshot_id, created_at, run_id);

CREATE TABLE scan_observations (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	observation_key TEXT NOT NULL, 
	origin TEXT NOT NULL, 
	kind TEXT NOT NULL, 
	file_id UUID NOT NULL, 
	payload_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	payload_digest TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id, observation_key), 
	CONSTRAINT ck_scan_observations_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=payload_json AND octet_length(canonical_text)<=262144), 
	CONSTRAINT ck_scan_observations_digest CHECK (payload_digest=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT fk_scan_observation_report FOREIGN KEY(org_id, snapshot_id, run_id) REFERENCES scan_reports (org_id, snapshot_id, run_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_scan_observation_file FOREIGN KEY(org_id, snapshot_id, file_id) REFERENCES source_files (org_id, snapshot_id, id) ON DELETE RESTRICT, 
	CONSTRAINT ck_scan_observation_key CHECK (observation_key=origin||':'||payload_digest AND octet_length(observation_key)<=80), 
	CONSTRAINT ck_scan_observation_kind CHECK ((origin IN ('semgrep','gitleaks') AND kind='native') OR (origin='dependencies' AND kind IN ('dependency_instance','unresolved_dependency')))
)

;
CREATE INDEX ix_scan_observations_file ON scan_observations (org_id, snapshot_id, run_id, file_id, observation_key);

CREATE TABLE finding_occurrences (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	id UUID NOT NULL, 
	file_id UUID NOT NULL, 
	observation_key TEXT NOT NULL, 
	category TEXT NOT NULL, 
	rule_id TEXT NOT NULL, 
	payload_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	payload_digest TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id, id), 
	CONSTRAINT ck_finding_occurrences_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=payload_json AND octet_length(canonical_text)<=32768), 
	CONSTRAINT ck_finding_occurrences_digest CHECK (payload_digest=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT fk_finding_report FOREIGN KEY(org_id, snapshot_id, run_id) REFERENCES scan_reports (org_id, snapshot_id, run_id) ON DELETE RESTRICT, 
	CONSTRAINT fk_finding_file FOREIGN KEY(org_id, snapshot_id, file_id) REFERENCES source_files (org_id, snapshot_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_finding_observation FOREIGN KEY(org_id, snapshot_id, run_id, observation_key) REFERENCES scan_observations (org_id, snapshot_id, run_id, observation_key) ON DELETE RESTRICT, 
	CONSTRAINT ck_finding_category CHECK (category IN ('source_pattern','secret_format','dependency_advisory')), 
	CONSTRAINT ck_finding_rule CHECK (rule_id ~ '^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$')
)

;
CREATE INDEX ix_findings_file ON finding_occurrences (org_id, snapshot_id, run_id, file_id, id);

CREATE TABLE finding_support (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	finding_id UUID NOT NULL, 
	evidence_id UUID NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id, finding_id, evidence_id), 
	CONSTRAINT fk_finding_support_occurrence FOREIGN KEY(org_id, snapshot_id, run_id, finding_id) REFERENCES finding_occurrences (org_id, snapshot_id, run_id, id) ON DELETE RESTRICT, 
	CONSTRAINT fk_finding_support_evidence FOREIGN KEY(org_id, snapshot_id, run_id, evidence_id) REFERENCES evidence_records (org_id, snapshot_id, run_id, id) ON DELETE RESTRICT
)

;
CREATE INDEX ix_finding_support_reverse ON finding_support (org_id, snapshot_id, run_id, evidence_id, finding_id);

CREATE TABLE verification_decisions (
	org_id UUID NOT NULL, 
	snapshot_id UUID NOT NULL, 
	run_id UUID NOT NULL, 
	finding_id UUID NOT NULL, 
	disposition TEXT NOT NULL, 
	payload_json JSONB NOT NULL, 
	canonical_text TEXT NOT NULL, 
	payload_digest TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	PRIMARY KEY (org_id, snapshot_id, run_id, finding_id), 
	CONSTRAINT ck_verification_decisions_json CHECK (canonical_text IS JSON OBJECT WITH UNIQUE KEYS AND canonical_text::jsonb=payload_json AND octet_length(canonical_text)<=8192), 
	CONSTRAINT ck_verification_decisions_digest CHECK (payload_digest=encode(sha256(convert_to(canonical_text,'UTF8')),'hex')), 
	CONSTRAINT fk_scan_decision_finding FOREIGN KEY(org_id, snapshot_id, run_id, finding_id) REFERENCES finding_occurrences (org_id, snapshot_id, run_id, id) ON DELETE RESTRICT, 
	CONSTRAINT ck_scan_decision_disposition CHECK (disposition IN ('proposed','supported-static','contradicted','unresolved'))
)

;
ALTER TABLE public.jobs ADD COLUMN scan_spec_id UUID;
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot','analyze_structure','scan_snapshot'));
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_scan CHECK((operation='scan_snapshot' AND scan_spec_id IS NOT NULL AND snapshot_id IS NOT NULL AND submission_id IS NULL AND analysis_spec_id IS NULL AND parent_job_id IS NULL) OR (operation<>'scan_snapshot' AND scan_spec_id IS NULL));
ALTER TABLE public.jobs ADD CONSTRAINT fk_job_scan_spec FOREIGN KEY(org_id,repository_id,snapshot_id,scan_spec_id,config_hash) REFERENCES public.scan_specs(org_id,repository_id,snapshot_id,id,config_hash) ON DELETE RESTRICT ON UPDATE NO ACTION;

"""


def upgrade() -> None:
    op.execute(SQL)


def downgrade() -> None:
    op.execute(r"""
DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.scan_specs) OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='scan_snapshot') THEN RAISE EXCEPTION 'scan history must be preserved'; END IF; END $$;
ALTER TABLE public.jobs DROP CONSTRAINT fk_job_scan_spec;
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_scan;
ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot','analyze_structure'));
ALTER TABLE public.jobs DROP COLUMN scan_spec_id;
DROP TABLE public.verification_decisions;
DROP TABLE public.finding_support;
DROP TABLE public.finding_occurrences;
DROP TABLE public.scan_observations;
DROP TABLE public.scan_reports;
DROP TABLE public.scan_attempt_specs;
DROP TABLE public.scan_specs;
""")
