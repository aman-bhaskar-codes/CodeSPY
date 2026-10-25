# Triage requests and bounded review queries

A triage POST requires an `Idempotency-Key` header containing 1–128 UTF-8 bytes. Keep the same key when retrying the same request after an uncertain network outcome. A changed decision, expected version or target needs a new key.

Replay is scoped to the current organization and principal. It returns the original response, even if another decision has since advanced the projection. Reusing a key with different input returns 409. Current repository write permission and sealed snapshot access are required before replay. A no-op records its response without adding an audit event. Response storage, audit events and projection updates share one transaction; failed requests do not consume a key.

The browser retains a key for an unchanged modal submission after an error. It generates a new key when the input changes. Closing the modal or reloading the page does not provide durable client-side recovery; an API caller can retain its key independently.

## Database compatibility

The new `0022_triage_commands` migration extends the existing immutable `api_commands` operation constraint. Apply it before enabling keyed triage writes. Existing migration files and audit records are unchanged. Downgrading refuses to restore the narrower constraint while retained triage command responses exist; it does not delete those responses. Upgrade, empty-command downgrade and re-upgrade passed on owned PostgreSQL 16. Retained command responses prevent downgrade, with the transaction and revision record preserved.

## Finding pages

Finding queries accept category, severity, current effective disposition and triage-state filters. All filters apply before matching totals and page limits. Limits are 1–100, default 50. Findings sort by their immutable UUID in ascending order. `total_count` counts all current matches, independent of the current page position.

Pass `next_cursor` back as `cursor` with the same filters and scope. Tokens are signed and bound to the organization, principal, repository, snapshot, run and filters. Malformed or mismatched tokens and invalid filters or limits return 422. Current authorization is checked on every request. Changing filters restarts pagination. Tokens use the application's existing ephemeral signing key and expire when the application instance restarts; multi-instance key configuration remains separate work.

## Triage history

History uses the same 1–100 limit, default 50, and returns `next_cursor`. Events sort by `(created_at, event_id)` descending, so events with identical timestamps have a deterministic order. The UI offers older and newest decisions without implying that the displayed page is the complete audit trail.

These are live queries, not a snapshot held across HTTP requests. Concurrent triage changes can alter finding filter membership and totals between pages. Disposition filters, counts and detail share the current-support projection; historical decisions remain separately available. Support availability changes can also alter membership between pages.

## Verification boundary

SQLite service and authenticated HTTP tests cover replay, no-op handling, rollback, permission checks, filtered counts and page boundaries. A synthetic Chrome fixture exercises actual modal retry-key behavior without calling a backend. The owned PostgreSQL regression suite passed 8 tests, including duplicate concurrent commands, current authorization, migration compatibility and retained-response downgrade refusal. These real database results are separate from synthetic and SQLite checks. See the [verification record](acceptance/review-queries.md).

Implementation choices reuse the existing codec and command store. PostgreSQL documents the need for a [unique ordering with LIMIT](https://www.postgresql.org/docs/16/queries-limit.html) and the transaction lifetime of [row locks](https://www.postgresql.org/docs/16/explicit-locking.html). No pagination or idempotency dependency was added.
