"""Unit tests for BudgetOperationsService funding, idempotency, and owner authorization."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.contracts.budget_operations import FundBudgetV1
from app.domain.errors import DuplicateConflict, ScopeViolation
from app.domain.scope import OrgRole, TenantContext
from app.models.budget_tables import budget_funding_receipts, embedding_budgets
from app.models.phase2_tables import PHASE2_TABLES
from app.models.tables import memberships, repositories
from app.services.budget_operations import BudgetOperationsService
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

grants = PHASE2_TABLES["repository_grants"]


@pytest_asyncio.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        memberships,
        repositories,
        grants,
        embedding_budgets,
        budget_funding_receipts,
    ]
    async with engine.begin() as conn:
        for table in tables:
            await conn.run_sync(table.create)
    fac = async_sessionmaker(engine, expire_on_commit=False)
    yield fac
    await engine.dispose()


async def seed_env(
    fac: async_sessionmaker[AsyncSession],
    role: OrgRole = OrgRole.OWNER,
    initial_balance: int | None = 100,
    initial_reserved: int = 100,
    initial_debt: int = 300,
) -> tuple[TenantContext, UUID]:
    org_id = uuid4()
    principal_id = uuid4()
    repo_id = uuid4()

    async with fac() as s:
        await s.execute(
            memberships.insert().values(org_id=org_id, principal_id=principal_id, role=role.value)
        )
        await s.execute(
            repositories.insert().values(
                org_id=org_id,
                id=repo_id,
                repository_key=str(repo_id),
                display_name="test-repo",
                source_kind="archive",
            )
        )
        await s.execute(
            grants.insert().values(
                org_id=org_id,
                repository_id=repo_id,
                principal_id=principal_id,
                permission="write",
            )
        )
        if initial_balance is not None:
            await s.execute(
                embedding_budgets.insert().values(
                    org_id=org_id,
                    repository_id=repo_id,
                    currency="USD",
                    balance_micros=initial_balance,
                    reserved_micros=initial_reserved,
                    debt_micros=initial_debt,
                    write_version=1,
                )
            )
        await s.commit()

    ctx = TenantContext(org_id=org_id, principal_id=principal_id, role=role)
    return ctx, repo_id


@pytest.mark.asyncio
async def test_fund_oracle_execution(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx, repo_id = await seed_env(
        factory, initial_balance=100, initial_reserved=100, initial_debt=300
    )
    service = BudgetOperationsService(factory)

    # Fund 200 -> (100, 100, 100)
    req1 = FundBudgetV1(
        request_key="fund-1",
        amount_micros=200,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="a" * 64,
    )
    rcpt1 = await service.fund(ctx, repo_id, req1)
    assert rcpt1.credited_micros == 0
    assert rcpt1.debt_repaid_micros == 200
    assert rcpt1.resulting_balance_micros == 100
    assert rcpt1.resulting_reserved_micros == 100
    assert rcpt1.resulting_debt_micros == 100

    # Fund 150 -> (150, 100, 0)
    req2 = FundBudgetV1(
        request_key="fund-2",
        amount_micros=150,
        currency="USD",
        reason="manual_adjustment",
        evidence_digest="b" * 64,
    )
    rcpt2 = await service.fund(ctx, repo_id, req2)
    assert rcpt2.credited_micros == 50
    assert rcpt2.debt_repaid_micros == 100
    assert rcpt2.resulting_balance_micros == 150
    assert rcpt2.resulting_reserved_micros == 100
    assert rcpt2.resulting_debt_micros == 0


@pytest.mark.asyncio
async def test_fund_idempotent_replay(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx, repo_id = await seed_env(factory, initial_balance=50, initial_reserved=0, initial_debt=0)
    service = BudgetOperationsService(factory)

    req = FundBudgetV1(
        request_key="replay-key",
        amount_micros=1000,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="c" * 64,
    )
    rcpt_first = await service.fund(ctx, repo_id, req)
    rcpt_second = await service.fund(ctx, repo_id, req)

    assert rcpt_first.audit_id == rcpt_second.audit_id
    assert rcpt_first.resulting_balance_micros == rcpt_second.resulting_balance_micros


@pytest.mark.asyncio
async def test_fund_conflicting_replay(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx, repo_id = await seed_env(factory, initial_balance=50, initial_reserved=0, initial_debt=0)
    service = BudgetOperationsService(factory)

    req = FundBudgetV1(
        request_key="conflict-key",
        amount_micros=1000,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="c" * 64,
    )
    await service.fund(ctx, repo_id, req)

    req_modified = FundBudgetV1(
        request_key="conflict-key",
        amount_micros=2000,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="c" * 64,
    )
    with pytest.raises(DuplicateConflict, match="funding request conflicts"):
        await service.fund(ctx, repo_id, req_modified)


@pytest.mark.asyncio
async def test_fund_denies_non_owner(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx, repo_id = await seed_env(
        factory, role=OrgRole.MEMBER, initial_balance=50, initial_reserved=0, initial_debt=0
    )
    service = BudgetOperationsService(factory)

    req = FundBudgetV1(
        request_key="any-key",
        amount_micros=500,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="d" * 64,
    )
    with pytest.raises(ScopeViolation, match="owner authorization required"):
        await service.fund(ctx, repo_id, req)


@pytest.mark.asyncio
async def test_init_budget_idempotent(factory: async_sessionmaker[AsyncSession]) -> None:
    ctx, repo_id = await seed_env(factory, initial_balance=None)
    service = BudgetOperationsService(factory)

    await service.init_budget(ctx, repo_id)
    async with factory() as s:
        row = (await s.execute(select(embedding_budgets))).mappings().one()
        assert row["balance_micros"] == 0
        assert row["reserved_micros"] == 0
        assert row["debt_micros"] == 0

    # Repeat init_budget does not reset or error
    await service.init_budget(ctx, repo_id)


@pytest.mark.asyncio
async def test_fund_sequential_protects_holds_and_repays_debt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ctx, repo_id = await seed_env(
        factory, initial_balance=200, initial_reserved=200, initial_debt=500
    )
    service = BudgetOperationsService(factory)

    req1 = FundBudgetV1(
        request_key="seq-1",
        amount_micros=300,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="1" * 64,
    )
    req2 = FundBudgetV1(
        request_key="seq-2",
        amount_micros=400,
        currency="USD",
        reason="initial_deposit",
        evidence_digest="2" * 64,
    )

    rcpt1 = await service.fund(ctx, repo_id, req1)
    rcpt2 = await service.fund(ctx, repo_id, req2)

    async with factory() as s:
        row = (await s.execute(select(embedding_budgets))).mappings().one()
        # initial: balance=200, reserved=200, debt=500
        # total funded = 300 + 400 = 700
        # debt repaid = 500, remaining debt = 0
        # cash credited = 200
        # new balance = 200 + 200 = 400
        # reserved = 200 (holds untouched)
        # available = 400 - 200 = 200
        assert row["balance_micros"] == 400
        assert row["reserved_micros"] == 200
        assert row["debt_micros"] == 0
        assert rcpt1.debt_repaid_micros + rcpt2.debt_repaid_micros == 500
        assert rcpt1.credited_micros + rcpt2.credited_micros == 200
