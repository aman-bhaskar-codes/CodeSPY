# ruff: noqa: E501
"""Immutable structural request and report storage."""

from alembic import op

revision = "0010_structural_specs"
down_revision = "0009_api_commands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    SET LOCAL lock_timeout='5s';
    CREATE TABLE public.analysis_specs (
      org_id UUID NOT NULL, id UUID NOT NULL, repository_id UUID NOT NULL,
      snapshot_id UUID NOT NULL, principal_id UUID NOT NULL, config_json JSONB NOT NULL,
      config_hash TEXT NOT NULL, toolchain_hash TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(org_id,id),
      CONSTRAINT uq_spec_scope UNIQUE(org_id,repository_id,snapshot_id,id,config_hash),
      CONSTRAINT fk_spec_snapshot FOREIGN KEY(org_id,repository_id,snapshot_id)
        REFERENCES public.source_snapshots(org_id,repository_id,id) ON DELETE RESTRICT,
      CONSTRAINT fk_spec_member FOREIGN KEY(org_id,principal_id)
        REFERENCES public.memberships(org_id,principal_id) ON DELETE RESTRICT,
      CONSTRAINT ck_spec_config CHECK(jsonb_typeof(config_json)='object' AND octet_length(config_json::text)<=98304),
      CONSTRAINT ck_spec_hashes CHECK(config_hash ~ '^[0-9a-f]{64}$' AND toolchain_hash ~ '^[0-9a-f]{64}$')
    );
    CREATE INDEX ix_analysis_specs_history ON public.analysis_specs(org_id,repository_id,snapshot_id,created_at,id);
    CREATE TABLE public.structural_reports (
      org_id UUID NOT NULL, snapshot_id UUID NOT NULL, run_id UUID NOT NULL,
      spec_id UUID NOT NULL, repository_id UUID NOT NULL, config_hash TEXT NOT NULL,
      report_json JSONB NOT NULL, report_sha256 TEXT NOT NULL, normalized_sha256 TEXT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(org_id,snapshot_id,run_id),
      CONSTRAINT fk_report_run FOREIGN KEY(org_id,snapshot_id,run_id)
        REFERENCES public.analysis_runs(org_id,snapshot_id,id) ON DELETE RESTRICT,
      CONSTRAINT fk_report_spec FOREIGN KEY(org_id,repository_id,snapshot_id,spec_id,config_hash)
        REFERENCES public.analysis_specs(org_id,repository_id,snapshot_id,id,config_hash) ON DELETE RESTRICT,
      CONSTRAINT ck_report_json CHECK(jsonb_typeof(report_json)='object' AND octet_length(report_json::text)<=4194304),
      CONSTRAINT ck_report_hashes CHECK(config_hash ~ '^[0-9a-f]{64}$' AND report_sha256 ~ '^[0-9a-f]{64}$' AND normalized_sha256 ~ '^[0-9a-f]{64}$')
    );
    CREATE INDEX ix_structural_reports_history ON public.structural_reports(org_id,repository_id,snapshot_id,created_at,run_id);
    ALTER TABLE public.jobs ADD COLUMN analysis_spec_id UUID NULL;
    ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
    ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot','analyze_structure'));
    ALTER TABLE public.jobs ADD CONSTRAINT ck_job_structure CHECK(
      (operation='analyze_structure' AND analysis_spec_id IS NOT NULL AND snapshot_id IS NOT NULL AND submission_id IS NULL)
      OR (operation<>'analyze_structure' AND analysis_spec_id IS NULL));
    ALTER TABLE public.jobs ADD CONSTRAINT fk_job_spec FOREIGN KEY(org_id,repository_id,snapshot_id,analysis_spec_id,config_hash)
      REFERENCES public.analysis_specs(org_id,repository_id,snapshot_id,id,config_hash) ON DELETE RESTRICT;
    """)


def downgrade() -> None:
    op.execute("""
    DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.analysis_specs) OR EXISTS(SELECT 1 FROM public.structural_reports)
      OR EXISTS(SELECT 1 FROM public.jobs WHERE operation='analyze_structure') THEN
      RAISE EXCEPTION 'structural history must be preserved'; END IF; END $$;
    ALTER TABLE public.jobs DROP CONSTRAINT fk_job_spec;
    ALTER TABLE public.jobs DROP CONSTRAINT ck_job_structure;
    ALTER TABLE public.jobs DROP CONSTRAINT ck_job_operation;
    ALTER TABLE public.jobs ADD CONSTRAINT ck_job_operation CHECK(operation IN ('admit_source','inventory_snapshot'));
    ALTER TABLE public.jobs DROP COLUMN analysis_spec_id;
    DROP TABLE public.structural_reports;
    DROP TABLE public.analysis_specs;
    """)
