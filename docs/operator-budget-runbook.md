# Model accounting and budget operations runbook

This document describes the operational commands, authorization rules, and execution
workflows for managing model and provider budgets, positive account funding, recovery,
and audited usage reconciliation.

## 1. Permission and security model

All financial records are protected by database role separation:

- **Runtime API (`p2_api`)**: Can reserve generation/embedding holds and cancel pre-dispatch reservations. Has no direct `INSERT`, `UPDATE`, `DELETE`, or `TRUNCATE` privileges on financial tables.
- **Worker Runtime (`p2_broker`)**: Can commit dispatch intents, mark calls dispatched, settle completed calls, and mark stale calls as uncertain holds. Has no direct DML privileges.
- **Trusted Operator (`p2_operator`)**: Can initialize zero-funded accounts, execute positive funding, publish price schedules, and perform audited reconciliations. Has no direct UPDATE or DELETE privileges; operations produce immutable audit receipts.

All mutations are routed through PostgreSQL security-definer routines with a fixed
`search_path = pg_catalog, public`.

## 2. Account initialization

Before any model reservation can succeed, a budget account must be explicitly initialized
for the repository.

```python
from app.domain.scope import TenantContext
from app.services.budget_operations import BudgetOperationsService

# Actor must hold OWNER role membership in the organization
ops = BudgetOperationsService(session_factory)
await ops.init_budget(
    actor=TenantContext(org_id=org_id, principal_id=owner_principal_id),
    repository_id=repository_id,
    currency="USD",
)
```

The account is created with zero spendable balance, zero reserved holds, and zero debt.
Ordinary execution requests cannot implicitly create or auto-fund accounts.

## 3. Explicit positive funding

To add spending authorization to an account:

```python
from app.contracts.budget_operations import FundBudgetV1
from app.services.budget_operations import BudgetOperationsService

ops = BudgetOperationsService(session_factory)
receipt = await ops.fund(
    actor=TenantContext(org_id=org_id, principal_id=owner_principal_id),
    repository_id=repository_id,
    request=FundBudgetV1(
        request_key="deposit-2026-10-25-001",
        amount_micros=10_000_000,  # USD 10.00
        currency="USD",
        reason="operator_initial_credit",
        evidence_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    ),
)
```

### Funding invariants
- **Priority debt repayment**: If outstanding debt exists, incoming funds repay debt first:
  `repayment = min(amount, debt)`.
- **Balance credit**: Only excess funds increase the spendable balance:
  `new_balance = old_balance + amount - repayment`.
- **Hold preservation**: Outstanding reservations remain untouched.
- **Idempotency**: Submitting the identical `request_key` returns the original immutable receipt.
  Submitting the same key with different parameters raises a duplicate conflict.

## 4. Price schedule publishing

Authoritative prices must be configured explicitly before dispatch. In-flight requests capture
the price schedule at reservation time; subsequent updates to global schedules do not alter
historical reservations.

```python
from app.contracts.model import GenerationPriceScheduleV1
from app.services.model_budget import ModelBudgetService

async with transaction(session_factory) as session:
    service = ModelBudgetService(session)
    await service.set_generation_price_schedule(
        provider="anthropic",
        model_id="claude-3-5-sonnet-20241022",
        schedule=GenerationPriceScheduleV1(
            provider="anthropic",
            model_id="claude-3-5-sonnet-20241022",
            effective_from=datetime.now(UTC),
            input_price_micros_per_million=3_000_000,
            output_price_micros_per_million=15_000_000,
            cache_read_price_micros_per_million=300_000,
            cache_write_price_micros_per_million=3_750_000,
        ),
    )
```

## 5. Bounded dispatch recovery

The `AccountingRecoveryWorker` handles stale in-flight reservations independently of
investigation job state:

```python
from app.contracts.accounting_recovery import AccountingRecoverySettingsV1
from app.workers.accounting_recovery import AccountingRecoveryWorker

settings = AccountingRecoverySettingsV1(
    enabled=True,
    interval_seconds=15.0,
    batch_limit=100,
    stale_age_seconds=300,
    max_provider_call_duration_seconds=180,
    recovery_margin_seconds=30,
)

worker = AccountingRecoveryWorker(session_factory, settings)
summary = await worker.tick()
# summary.scanned: number of candidate reservations reviewed
# summary.held: number transitioned to uncertain holds
# summary.failed_scopes: number of tenant scopes that encountered transient errors
```

- **Stale age threshold**: Must be >= 60 seconds and strictly greater than
  `max_provider_call_duration_seconds + recovery_margin_seconds`.
- **Conservative hold**: Stale calls become `uncertain_hold`. Recovery never releases reserved funds
  or re-dispatches inference.

## 6. Audited reconciliation

Uncertain holds can be resolved once by submitting verified usage evidence:

```python
from app.contracts.model import TokenUsageV1
from app.services.model_reconciliation import ModelReconciliationService

async with transaction(session_factory) as session:
    recon = ModelReconciliationService(session)
    receipt = await recon.reconcile(
        actor=TenantContext(org_id=org_id, principal_id=owner_principal_id),
        repository_id=repository_id,
        reservation_id=reservation_id,
        request_key="recon-2026-10-25-001",
        evidence_digest="a" * 64,
        usage=TokenUsageV1(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            reasoning_tokens=0,
        ),
        provider_request_id="req_verified_123",
    )
```

- All token categories must be measured and explicitly stated (including zero counts).
- Settlement recalculates charges using the price snapshot captured at initial reservation time.
- Receipts are immutable and auditable.
