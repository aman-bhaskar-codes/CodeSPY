# Durable repository investigations

The application contains repository-scoped investigation commands, immutable
specifications, node execution contracts, retained model results, report storage
and read-only status/result projections. These build on the existing jobs,
attempts, outbox, accounting gateway and admitted-source services.

Creation returns a pending investigation. Starting configured work returns a
202 response with durable job references. A missing model runtime/adapter returns
`inference_unavailable`; no default provider or spending capacity is created.
The legacy request-driven implementation remains a compatibility path and does
not establish durable-worker qualification.

Start, resume and retry require the original requesting principal with current
write permission. Another writer creates their own investigation. Cancellation
is a current-writer action. It cannot refund a potentially dispatched model call;
known charges and uncertain holds remain the accounting gateway's responsibility.
Manual retry creates a new investigation and preserves the original history.

## Report publication

The report finalizer uses the shared publication executor with a ten-second total
forward-work budget, starting before connection checkout. PostgreSQL statement,
lock and idle safeguards are five, two and five seconds respectively. Cleanup
has a separate bounded allowance and cannot extend publication authority.

Publication rechecks current repository write permission, job/spec scope, current
attempt, fence, owner and active attempt state. It requires at least 65 seconds
of lease and 15 seconds of investigation deadline at entry. Database time is
rechecked before terminal writes. Exact replay compares investigation, job,
attempt, fence, specification and draft digest. Ambiguous commit recovery reads
the durable receipt rather than repeating model work.

Additive revision `0033_investigation_publication_guards` requires live worker
context for report/receipt insertion. Deferred guards require a consistent report,
receipt, terminal job/attempt and completed investigation summary with live lease
and deadline. The broker receives only the investigation summary column updates
needed for this operation. Historical migrations remain unchanged.

Unsupported report verdict vocabulary is conservatively projected as
`indeterminate`; it is never promoted to a supported conclusive outcome.
This normalization is not semantic verification of a security claim.

## Verification and remaining limits

Local PostgreSQL tests use a separate restricted broker connection for publication
and replay. They also exercise actual database lease expiry, multiple individually
short statements exceeding the total budget, rollback without a visible report,
and deferred rejection of an orphan report. SQLite tests provide additional
contract coverage; they do not qualify PostgreSQL privileges or races.

An attempt-local composition factory now connects the configured gateway, SQL
checkpoint and receipt stores, retained encrypted outcomes, read-only tool adapter
and report finalizer. Each attempt receives its own gateway and bound dispatch
authority. Executor reads commit before provider I/O; the actual attempt identity
is retained rather than synthesizing an owner. Optional finding/run/chunk-set
identities survive the operation boundary.

The composed path checks retained source and, for finding targets, delegates to
existing current-support services with configured scanner capabilities. Missing
finding capabilities fail closed. The shared retained-source predicate is also
used by memory, avoiding a second interpretation of source availability.

Revision `0034_scoped_model_result_reads` removes direct API/broker ciphertext
SELECT. Exact scoped reads bind actor, repository, investigation, reservation,
call, request/profile identity and database expiry. Bounded result cleanup uses a
constrained routine. Shared SQL logins still trust application-supplied authenticated
identity; this is not arbitrary-SQL end-user isolation.

Restricted-role tests run both short and specialist workflows against a synthetic
HTTP provider and the real isolated source reader. Encrypted effects bind each
model/tool/draft record to actor, repository, snapshot, spec and execution identity.
Checkpoints contain reference IDs and digests. Recovery after committed tool output
and after a saved draft reuses the same records without another model dispatch,
then publishes one report and proposes one review candidate.

Delivered excerpt verification checks the exact receipt hash, scope, line range
and quote. Unverified claims remain unsupported; a true-positive label cannot
bypass grounding. Retention is seven days, with bounded payload purge preserving
minimal digests. Missing legacy execution history requires an explicit restart.

`build_worker_runtime` connects existing operations, model maintenance and the
optional report-candidate consumer through injected configuration. It does not
read keys or discover cloud access. The API role can lock an authorized sealed
snapshot through a scoped routine without receiving snapshot UPDATE permission.
The complete upload-to-investigation browser journey remains unqualified.

Model transports are qualified offline with synthetic responses. No live provider
quality, AWS access, account entitlements, deployment or complete product readiness
is established here. The owner supplies credentials and performs live activation.

## Local regression evidence

The owned PostgreSQL accounting, model-connection and memory suites passed 77
cases. After the final proposal-expiry repair, four source/proposal and connected
investigation cases passed again. The investigation/memory/accounting follow-up
passed 263 cases with five guarded cases skipped. Strict application typing
passed for 280 files; touched Python lint and diff whitespace checks passed.

Proposal ownership includes database expiry. Renewal cannot revive an expired
claim, processing has a thirty-second total budget, and a missed acknowledgment
rolls back candidate effects. Completion counts represent committed effects.
Independent review was unavailable because the review agent hit a usage limit;
these changes have author self-review and test evidence, not an independent
security verdict. Live providers and the complete browser journey remain outside
this qualification.
