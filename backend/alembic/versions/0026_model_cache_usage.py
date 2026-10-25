"""Retain measured cache usage without inventing historical token counts."""

from alembic import op

revision = "0026_model_cache_usage"
down_revision = "0025_budget_debt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
ALTER TABLE public.model_spend_ledger
 ADD COLUMN cache_read_tokens bigint,
 ADD COLUMN cache_write_tokens bigint,
 ADD CONSTRAINT ck_model_cache_read_nonneg
   CHECK(cache_read_tokens IS NULL OR cache_read_tokens >= 0),
 ADD CONSTRAINT ck_model_cache_write_nonneg
   CHECK(cache_write_tokens IS NULL OR cache_write_tokens >= 0);
""")


def downgrade() -> None:
    op.execute("""
SET LOCAL lock_timeout='5s';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.model_spend_ledger
           WHERE cache_read_tokens > 0 OR cache_write_tokens > 0) THEN
   RAISE EXCEPTION 'cannot downgrade measured cache usage' USING ERRCODE='23514';
 END IF;
END $$;
ALTER TABLE public.model_spend_ledger
 DROP CONSTRAINT ck_model_cache_read_nonneg,
 DROP CONSTRAINT ck_model_cache_write_nonneg,
 DROP COLUMN cache_read_tokens,
 DROP COLUMN cache_write_tokens;
""")
