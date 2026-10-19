"""Durable idempotency for repository creation and GitHub binding commands."""

from alembic import op

revision = "0009_api_commands"
down_revision = "0008_worker_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE public.api_commands (
      org_id UUID NOT NULL, principal_id UUID NOT NULL, request_key TEXT NOT NULL,
      operation TEXT NOT NULL, request_hash TEXT NOT NULL, response JSONB NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(org_id,principal_id,request_key),
      CONSTRAINT fk_command_membership FOREIGN KEY(org_id,principal_id)
        REFERENCES public.memberships(org_id,principal_id) ON DELETE RESTRICT,
      CONSTRAINT ck_command_key CHECK(octet_length(request_key) BETWEEN 1 AND 128),
      CONSTRAINT ck_command_hash CHECK(request_hash ~ '^[0-9a-f]{64}$'),
      CONSTRAINT ck_command_operation CHECK(operation IN ('repository_create','github_bind')),
      CONSTRAINT ck_command_response CHECK(jsonb_typeof(response)='object'
        AND octet_length(response::text)<=65536)
    );
    CREATE TRIGGER guard_command_immutable BEFORE UPDATE OR DELETE ON public.api_commands
      FOR EACH ROW EXECUTE FUNCTION public.codespy_reject_mutation();
    """)


def downgrade() -> None:
    op.drop_table("api_commands")
