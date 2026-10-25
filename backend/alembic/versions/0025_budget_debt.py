"""Preserve unfunded provider charges without consuming other active holds."""

from alembic import op

revision = "0025_budget_debt"
down_revision = "0024_model_request_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.embedding_budgets
 ADD COLUMN debt_micros bigint NOT NULL DEFAULT 0,
 ADD CONSTRAINT ck_budget_debt_nonneg CHECK(debt_micros >= 0),
 ADD CONSTRAINT ck_budget_debt_cash CHECK(debt_micros=0 OR balance_micros=reserved_micros);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.embedding_budgets WHERE debt_micros > 0) THEN
   RAISE EXCEPTION 'cannot downgrade outstanding budget debt' USING ERRCODE='23514';
 END IF;
END $$;
ALTER TABLE public.embedding_budgets
 DROP CONSTRAINT ck_budget_debt_cash,
 DROP CONSTRAINT ck_budget_debt_nonneg,
 DROP COLUMN debt_micros;
""")
