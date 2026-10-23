"""Allow durable human-review command responses without changing audit history."""

from alembic import op

revision = "0022_triage_commands"
down_revision = "0021_investigations_and_checkpoints"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
SET LOCAL lock_timeout='5s';
ALTER TABLE public.api_commands DROP CONSTRAINT ck_command_operation;
ALTER TABLE public.api_commands ADD CONSTRAINT ck_command_operation
CHECK(operation IN ('repository_create','github_bind','finding_triage'));
"""


def upgrade() -> None:
    op.execute(UPGRADE_SQL)


def downgrade() -> None:
    # Restoring the narrower constraint refuses downgrade if retained commands need it.
    op.execute("""
    SET LOCAL lock_timeout='5s';
    ALTER TABLE public.api_commands DROP CONSTRAINT ck_command_operation;
    ALTER TABLE public.api_commands ADD CONSTRAINT ck_command_operation
    CHECK(operation IN ('repository_create','github_bind'));
    """)
