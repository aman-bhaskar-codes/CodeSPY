# Model accounting and budget operations

Budget operations are internal, owner-authorized service calls. They do not create
provider credit, verify a cloud balance or enable a model. No public funding or
usage-reconciliation endpoint is described here.

## Authority and prerequisites

Use a trusted operator session factory with the reviewed database grants. API and
broker roles use constrained financial routines rather than direct financial-table
DML. Operator price installation has its own grants; do not assume every operator
operation uses the same security-definer routine.

`TenantContext` comes from authenticated application authority and contains `org_id`,
`principal_id` and `role`. Supplying an owner label cannot override current repository
permission checks. Zero-account initialization requires current repository write
access and the necessary operator database grants; the service does not enforce
an additional owner-role check for that zero-balance operation. Funding and audited
reconciliation enforce current owner authority. Runtime SQL logins are trusted
service identities, not independent end-user authentication boundaries.

Inspect [accounting evidence](acceptance/model-accounting.md) before relying on a
particular schema/role combination. Runtime/provider activation is separately
controlled by the [model configuration guide](operator-model-runbook.md).

## Initialize and fund

These are integration examples for an owner-controlled application context, not
standalone scripts. `operator_factory`, `actor` and `repository_id` must already be
bound to authorized resources. No credential discovery is performed.

```python
from app.contracts.budget_operations import FundBudgetV1
from app.services.budget_operations import BudgetOperationsService

ops = BudgetOperationsService(operator_factory)
await ops.init_budget(actor=actor, repository_id=repository_id, currency="USD")
receipt = await ops.fund(
    actor=actor,
    repository_id=repository_id,
    request=FundBudgetV1(
        request_key=approved_request_key,
        amount_micros=approved_amount_micros,
        currency="USD",
        reason="initial_deposit",
        evidence_digest=verified_evidence_digest,
    ),
)
```

Initialization creates a zero-funded account. Funding requires a positive amount;
allowed reasons are `initial_deposit`, `manual_adjustment` and `grant_replenishment`.
Amounts are integer USD micros, not tokens. An evidence digest is a SHA-256 hex
identity for independently retained evidence; do not substitute an empty placeholder.

Funding repays debt first. Only the remainder increases spendable balance, and
existing holds remain intact. Identical scoped request-key replay returns the
original receipt; conflicting replay is rejected. Funding is ledger authorization,
not a payment or provider balance check.

## Install immutable pricing

`ModelBudgetService(session).set_generation_price_schedule(schedule)` accepts one
`GenerationPriceScheduleV1`. The schedule identifies provider, model, immutable
version, effective/expiry timestamps and input/output/thinking/cache prices in
micros per 1,000 tokens. Use owner-qualified prices for the exact provider counters.
Do not copy stale example model IDs or per-million rates into this contract.

Price installation is separate from profile enablement and funding. Reservations
capture their price version; later configuration cannot silently reprice history.
Unsupported usage categories remain held for review rather than settled as free.

## Bounded recovery

`AccountingRecoveryWorker(factory, settings).tick()` uses
`AccountingRecoverySettingsV1`. Defaults are disabled, 15-second interval, batch
limit 100, stale age 300 seconds and recovery margin 30 seconds. Enabling recovery
requires a bounded `max_provider_call_duration_seconds`; stale age must exceed that
duration plus the margin and be at least 60 seconds.

The configured model maintenance pass is wired through the existing worker runtime.
It also handles stale shared embedding dispatch markers and bounded continuation
cleanup. Use [worker composition](operator-investigation-runbook.md#worker-composition-and-recovery)
rather than starting a duplicate scheduler.

A stale possibly dispatched call becomes `uncertain_hold`. Recovery does not resend
inference, infer zero usage or release funds because time elapsed. An unsent abandoned
reservation may require reviewed release through existing authority; never edit
ledger rows directly. Queue failure and investigation cancellation do not establish
that a provider call was unsent.

## Audited usage reconciliation

Reconciliation requires actual independently verified usage evidence:

```python
from app.contracts.model_reconciliation import ReconcileModelUsageV1
from app.services.model_reconciliation import ModelReconciliationService

receipt = await ModelReconciliationService(operator_factory).reconcile(
    actor=actor,
    repository_id=repository_id,
    request=ReconcileModelUsageV1(
        reservation_id=reservation_id,
        request_key=approved_request_key,
        provider_request_id=verified_provider_request_id,
        evidence_digest=verified_evidence_digest,
        usage=verified_usage,
        reason="provider_usage_receipt",
    ),
)
```

`verified_usage` is a `TokenUsageV1` for that exact call. The alternative reason is
`operator_usage_confirmation`. Reconciliation uses captured pricing and preserves
other reservations; resulting debt and provisional estimates remain explicit.
Matching replay is idempotent. It is not permission to overwrite an earlier audited
outcome, invent provider evidence or retry a potentially paid call.

No external billing feed or automated usage-confirmation source is implemented.
Automatic conservative recovery and manual evidence-backed reconciliation are
different mechanisms.

## Diagnostics, retention and cleanup

Use scoped numeric receipts, reservation state and redacted failure categories.
Do not log prompts, source, provider headers or encrypted continuation bodies.
Configuration validation and synthetic tests are offline checks; model availability
does not probe providers. No new standalone operator CLI is implied by these Python
service examples.

Retain the owner-managed encryption key for the lifetime of required execution
records. Stop owned workers before disposable database cleanup, and use the
harness ownership manifest. Never remove unrelated volumes or manually erase
financial history to make a test pass.

Recorded tests and role boundaries are in [accounting acceptance](acceptance/model-accounting.md)
and [model connection evidence](acceptance/model-connections.md). This documentation
update executes no funding, reconciliation, provider, database or service operation.
Live usage-counter interpretation, provider quality and hosted deployment remain
owner-qualified.
