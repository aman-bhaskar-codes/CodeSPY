# Model accounting transitions

The existing generation budget service now rejects request-key replay with changed
principal, repository, call identity, provider/model or reserved usage. Replays are
rechecked after the repository budget lock. Release and uncertainty transitions
lock the reservation before acting: potentially dispatched work retains its hold,
repeated uncertainty does not append duplicate provisional ledger entries, and
terminal reservations cannot reopen. Untouched reservations cannot settle.

Verification on 25 October 2026, reference 83767b1 plus local changes: eleven new
regressions first failed, then all 43 investigation unit tests passed. Two real
PostgreSQL tests passed in 3.14 seconds for concurrent identical reservations and
concurrent duplicate release preserving another hold. These storage tests use the
migration role and do not establish production API-role permissions. No real
provider, SDK, token pricing or model connectivity is qualified.

Factory-backed gateway calls now commit reservation and dispatch intent in a
short independent transaction before invoking the adapter. Settlement and uncertain
holds use another independent transaction. The investigation HTTP route initializes
its accounting configuration before orchestration and injects the existing session
factory. Current write permission is checked at accounting entry. Session-bound
compatibility is restricted to the fake provider; external calls require durable
accounting wiring.

Cancellation is caught explicitly, an uncertain hold is attempted within five
seconds, and the original exception is re-raised. If that attempt fails, the
committed dispatched reservation continues holding capacity. Old intent/dispatched
rows can be conservatively marked uncertain by a bounded internal recovery method
using database time and SKIP LOCKED. It never releases funds or replays inference.
The method is a recovery seam, not a claim that a periodic reconciliation worker
is running. Slow active calls can still settle from the uncertain state.

This remains partial accounting hardening. Measured input/category usage caps,
invoice reconciliation and operational funding controls,
production-role privileges and durable investigation jobs still need qualification
before external model activation. The orchestration/checkpoint transaction still
spans the investigation, but its rollback no longer erases factory-backed accounting.
No migrations or public transport contracts changed.

The final affected backend regression selection passed **960 tests** in 29.30
seconds. Strict typing passed for 225 application files; touched accounting code
and tests passed Ruff. Frontend/schema checks were not rerun because this
increment changes no public transport or frontend code.

## Durable dispatch verification

The cancellation regression first failed; four PostgreSQL durability/recovery
regressions first failed on the missing interfaces, and the revoked-writer
regression failed before its authority check. Final PostgreSQL command: **eight
passed in 8.88 seconds**, including committed state observed before simulated
inference, success/error/cancellation survival across an outer rollback, recovery
idempotency, revoked-writer denial and real HTTP investigation wiring. Models
remain explicitly simulated; tests use the migration-owner role.

Final affected backend selection: **960 passed in 24.11 seconds**. Strict typing
passed for 225 application files; Ruff and whitespace checks passed. Frontend
and schema regeneration were not rerun because transport contracts are unchanged.

## Pricing and reservation limits

Price versions are immutable through the service: exact repeated writes succeed,
but changed rates or effective dates require a new version. This is application
enforcement; privileged SQL is not guarded by a new trigger.

The gateway reserves the output limit it dispatches. Unspecified limits use the
routing requirement; explicit limits replace the output estimate before routing.
Invalid limits reject before accounting. The router checks declared model output
capacity and estimated input-plus-output context capacity. Actual prompt-token
measurement and reasoning/cache category ceilings remain unqualified.

Settlement replay must match the recorded charge, input/output/thinking usage and
provider request identity; conflicting replay cannot silently succeed or add a
second charge. Persisted request digests and per-invocation price snapshots are
now described below.

Twelve new regressions failed before implementation. Final affected backend:
**972 passed in 26.93 seconds**. Real PostgreSQL accounting suite: **nine passed
in 9.45 seconds**, including HTTP/fake-model and independent-transaction checks.
Strict typing passed for 225 application files; Ruff and whitespace checks passed.
No migrations, public schemas, SDKs or frontend changed; no real provider was called.

## Persisted invocation identity

New gateway calls capture a scoped digest of messages, tools, schema, output limit,
temperature, routing requirements and disclosure policy. The reservation stores
that digest and the selected price snapshot; raw prompts and source text are not
stored in accounting records. The gateway reads the reservation's captured price
before inference. Later changes to the global price schedule do not change the
captured price used by historical reservations.

The additive migration `0024_model_request_identity` preserves existing rows with
both fields absent. Legacy rows are not assigned invented digests and cannot be
silently rebound. New paired fields must agree on provider/model scope. Database
triggers reject bound identity/binding updates and deletion. This protects ordinary
UPDATE/DELETE operations, not a superuser disabling database guards.

Downgrade refuses to remove identity fields when bound reservations exist. Empty
or legacy-only databases can roundtrip while preserving existing reservation rows.
The JSON mapping writes absent snapshots as SQL NULL rather than JSON null.

This captures request identity and pricing; provider usage measurement, invoice
reconciliation scheduling and live provider qualification remain pending.

Final identity qualification on 25 October 2026: **974 affected backend tests
passed in 29.18 seconds**, and **18 real PostgreSQL accounting/migration checks
passed in 19.50 seconds**. Strict typing passed for 225 application files; Ruff
and whitespace checks passed. Tests include raw identity/binding mutation and
deletion rejection, incomplete metadata rejection, captured-price isolation from
global schedule changes, preserved legacy rows and guarded downgrade. Models
remain simulated and storage tests use the migration-owner role. No live provider
or production-role qualification is claimed.

## Shared-budget overruns

The additive `0025_budget_debt` migration records unfunded charges explicitly.
Both generation and embedding accounting use one typed integer-arithmetic helper.
Settlement preserves other active holds and records the exact known charge in the
existing ledger/reservation. Any shortfall becomes debt rather than a discarded
negative balance or a failed settlement. New reservations are denied while debt
remains, including zero-cost generation reservations. Releasing another hold
applies newly freed cash to existing debt without recording a second charge.

The invariant is funded balance >= outstanding holds >= 0. If debt exists, funded
balance equals outstanding holds; there is no spendable cash alongside unpaid
local exposure. Completing a hold preserves net balance-minus-debt less the exact
charge. This is local budget accounting, not a payment or a verified provider bill.
Downgrade refuses outstanding debt. Zero-debt accounts retain cash and holds through
migration roundtrip. Operational funding/top-up interfaces and invoice reconciliation
remain separate work.

Five regressions failed before implementation. Final affected backend: **979 passed
in 32.02 seconds**; real PostgreSQL accounting/migration suite: **22 passed in
28.22 seconds**. Tests include concurrent overrun settlement, generation/embedding
cross-accounting, intact unrelated holds and protected downgrade. Strict typing
passed for 226 application files; Ruff and whitespace checks passed. No frontend
or public transport changed; models remained simulated and storage tests used the
migration-owner role.

## Cache usage retention

The additive `0026_model_cache_usage` migration retains measured cache-read and
cache-write tokens in generation settlement records. The gateway forwards both
categories, and settlement replay compares them alongside existing usage and
charge fields. Historical rows and provisional uncertain holds retain NULL cache
counts: their cache usage is unknown. A legacy settlement without measured cache
counts cannot establish an exact replay. Downgrade refuses nonzero measured cache
usage rather than silently discarding it.

Observed checks: **980 affected backend tests passed in 24.81 seconds**;
**23 real PostgreSQL accounting/migration tests passed in 27.87 seconds**;
strict typing passed across 226 application files; Ruff and whitespace checks
passed. The new settlement regression failed before implementation. All six
authored files matched the executable test copy. Storage qualification used the
migration-owner role and simulated model responses. Live provider normalization,
invoice verification, audited manual reconciliation and production-role
qualification remain unexecuted. No frontend or public API contracts changed.

## Audited uncertain-call reconciliation

An internal `ModelReconciliationService` now accepts owner-supplied verified usage
and an evidence digest for an uncertain model reservation. It checks current owner
membership and repository write permission, repeats authorization after reservation
lock acquisition, and uses the captured historical price. Authorization is checked
within the transaction; membership revocation is not locked through commit.
Callers must verify all usage categories, including explicit zero categories, before
submission. An evidence digest is a reference, not automatic invoice verification.

Budget/debt changes, final usage ledger and the reconciliation receipt commit in one
independent transaction. Exact same-key replay returns the original receipt after
fresh authorization; changed replay and a new key for an already settled call are
rejected. The audit binds actor, request, reservation, evidence, usage and price.
Database guards reject audit mutation and receipts inconsistent with the final
settlement, and refuse a downgrade that would erase reconciliation history.
Uncertain estimated ledger records remain marked as provisional; consumers must
not sum them as additional settled charges. Legacy reservations without captured
prices remain unsupported. No provider is called or retried during reconciliation.

Observed final checks: **30 real PostgreSQL accounting/migration tests passed in
34.06 seconds**, including concurrent identical reconciliation, rollback on injected
audit failure, revoked access, current-owner demotion, immutable audit rows,
captured-price settlement, forged receipt rejection and guarded downgrade.
**980 affected backend tests passed in 28.27 seconds**; strict typing passed for
228 application files; Ruff and whitespace checks passed. The initial new tests
could not collect before the missing contract/service existed; this is not a claim
that each assertion was independently observed failing before implementation.

An unrestricted backend invocation reported **1,066 passed, 253 skipped and 76
fixture-setup errors**; the broader guarded legacy database/emulator fixtures were
not configured for that command (including the required loopback port 55432).
The full suite is therefore not qualified. The separately configured owned
PostgreSQL accounting harness passed. Storage checks use the migration-owner role
and simulated provider failures/usage. Production-role privileges, live invoice
verification, public activation and automatic reconciliation remain unqualified.
