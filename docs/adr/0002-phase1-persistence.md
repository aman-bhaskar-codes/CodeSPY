# ADR 0002: Local persistence foundation

Starting commit: 04de7aa4fbb5807de9e409bf82229d86b888e3b4; branch feat/phase1-database. Owner authorized implementation on 18 October 2026. No existing application code or migrations found locally; only main exists locally/remotely in cached refs. Earlier blueprint completion assumption is superseded by this plan.

Implement twelve scoped tables, explicit four-revision history, database lifecycle guards, async transaction services and a disposable PostgreSQL test harness. Contracts only: findings, jobs, leases, events and embeddings. Defer authentication, ingestion, scanners, workers, retrieval, frontend and hosted operation. Installed Arq/Redis/model dependencies remain dormant. Preserve ADR 0001's OpenTofu direction.

Retain existing uncommitted blueprint copy/preferences/roadmap updates. Leave unrelated infra/searxng/settings 2.yml untouched and untracked. Developer volumes and .env are not test inputs. AWS, API keys and Chrome remain prohibited. Only disposable local PostgreSQL tests may start; shut them down at handoff. Add the plan's requested PR/manual database validation without re-enabling old setup schedules.

Implementation rulings: table declarations and explicit ORM mappings are centralized in models/tables.py; historical revisions duplicate frozen DDL rather than importing it. Tenant-scoped advisory transaction locks serialize request-key creation without granting UPDATE on identity tables. Graph pages carry typed edge/node cursors, with endpoint repetition documented. Before publication, the entire stored result identity set must match the canonical batch, including staged repository inserts. Independent review regressions cover omitted records, SQL NULL coverage, and partial-prepare cleanup.

Session and locking behavior qualified against SQLAlchemy 2.0 and PostgreSQL 16 documentation: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html and https://www.postgresql.org/docs/16/explicit-locking.html . Locked versions were retained.

The initial index set retains the forward covering index: the measured forward/reverse query shapes and uniqueness constraint are distinct responsibilities. The unique relation index also enforces deduplication and is not removed based only on prefix similarity. Catalog drift and raw CHECK/trigger regression tests remain separate checks.
