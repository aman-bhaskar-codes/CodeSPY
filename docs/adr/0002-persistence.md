# ADR 0002: Scoped persistence and atomic publication

The persistence core uses twelve mapped tables and four independent Alembic revisions through `0004_guards`. Additive revisions extend ingestion, jobs, identity and structural reports without changing the frozen core DDL.

Services own fresh async sessions and transactions; repositories never commit. Snapshots seal their manifest atomically. Result publication validates the complete stored identity set against the canonical batch before changing terminal state. Scope keys bind organizations, repositories, snapshots and runs.

Table declarations and explicit ORM mappings are centralized in `backend/app/models/tables.py`; historical revisions contain frozen DDL instead of importing mutable metadata. Tenant-scoped advisory transaction locks serialize request-key creation without granting UPDATE on identity tables. Typed graph cursors support edges and disconnected nodes, with endpoint repetition explicit.

Regression evidence covers omitted result records, SQL NULL coverage, edgeless graph continuation and recovery after partial test-resource preparation. Forward covering indexes support measured query shapes; uniqueness indexes also enforce identity and are not removed solely because their prefixes overlap another index.

The test harness uses separately owned PostgreSQL resources and generated credentials. Developer volumes and `.env` files are not test inputs. Runtime roles have no DDL or trigger-bypass authority. See [database operations](../database.md) and [qualification evidence](../acceptance/persistence.md).

Reference behavior: [SQLAlchemy async sessions](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html) and [PostgreSQL locking](https://www.postgresql.org/docs/16/explicit-locking.html).
