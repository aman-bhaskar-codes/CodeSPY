# Agentic Security Investigations Operator Runbook

This runbook provides operational instructions for managing the lifecycle, worker operations,
financial budget controls, and recovery mechanisms of CodeSPY's Agentic Security Investigations subsystem.

---

## 1. Permission and Role Architecture

The investigation subsystem enforces strict database role boundaries in PostgreSQL:

- **API Runtime (`p2_api`)**:
  - Creates investigation records and job envelopes.
  - Subscribes to events and serves read-only queries.
  - Can trigger `cancel`, `resume`, and `retry` commands.
  - Cannot alter published reports, bypass tenancy, or acquire worker execution leases.
- **Worker Broker (`p2_broker`)**:
  - Leases queued jobs with transactional fences and database time checks.
  - Records step checkpoints, model replays, and verified evidence receipts.
  - Commits atomic final report publication.
  - Cannot create user-facing accounts or execute unleased modifications.
- **Operator Role (`p2_operator`)**:
  - Administrative role used for inspection, manual dead-letter resolution, and audited reconciliations.

---

## 2. Enqueueing and Starting an Investigation

All investigations target an immutable snapshot of an admitted code repository.

### Step 1: Create Investigation Record

```http
POST /api/v1/repositories/{repository_id}/investigations/v2
Content-Type: application/json

{
  "repository_id": "9f8b4a2e-4b6c-482a-a71f-0e1d8892a014",
  "snapshot_id": "3c5f2b8a-9e1d-482a-a71f-2e1d8892a099",
  "target_type": "repository",
  "query": "Investigate potential SSRF in webhook delivery handlers",
  "mode": "deep",
  "depth": "standard",
  "max_steps": 10,
  "budget_limit_micros": 5000000
}
```

*Response (201 Created)*: Returns `InvestigationSummaryV2` with `status: "queued"`.

### Step 2: Enqueue Execution Job

```http
POST /api/v1/repositories/{repository_id}/investigations/{investigation_id}/execute
Content-Type: application/json
Idempotency-Key: req-2026-10-26-001

{
  "request_key": "req-2026-10-26-001"
}
```

*Response (202 Accepted)*: Returns `InvestigationStartResponseV1` with `job_id`, `status: "queued"`, and `request_key`.
Synchronous execution is rejected; the job is processed asynchronously by leased background workers.

---

## 3. Monitoring Lifecycle and Events

### Fetch Status & Detail

```http
GET /api/v1/repositories/{repository_id}/investigations/{investigation_id}/v2
```

Returns `InvestigationDetailResponseV2` containing:
- `summary`: Current status, step progress, verdict, spent/held micros.
- `job`: Worker state (`queued`, `running`, etc.), attempt count, deadline.
- `available_actions`: Tuple of permitted actions based on current state (e.g. `["cancel"]`).
- `claims_count` & `evidence_count`: Counts of verified findings and supporting artifacts.

### Stream Real-Time Events (SSE)

```http
GET /api/v1/repositories/{repository_id}/investigations/{investigation_id}/events/v2?cursor=0
Accept: text/event-stream
```

Streams events with strictly monotonic sequences. If client reconnects after a disconnect, provide the last received `sequence` as the `cursor` query parameter.

---

## 4. Lifecycle Interventions

### Cancel a Running or Queued Investigation

When an investigation must be halted immediately (e.g. user requested, or excessive step count):

```http
POST /api/v1/repositories/{repository_id}/investigations/{investigation_id}/cancel
Content-Type: application/json

{
  "reason": "Operator cancelled due to erroneous query"
}
```

*Action*: Immediately revokes the job lease fence, sets investigation status to `cancelled`, and releases held budget reserves.

### Resume an Investigation Awaiting Human Decision

When an investigation encounters an ambiguous branching decision, it pauses in `awaiting_human_decision`:

```http
POST /api/v1/repositories/{repository_id}/investigations/{investigation_id}/resume
Content-Type: application/json

{
  "decision": "proceed_with_deep_inspection"
}
```

*Action*: Verifies the caller matches the original initiating principal or holds repository ownership, advances the node state, and re-enqueues execution.

### Retry a Failed or Cancelled Investigation

To rerun an investigation that failed or was cancelled:

```http
POST /api/v1/repositories/{repository_id}/investigations/{investigation_id}/retry
Content-Type: application/json
Idempotency-Key: retry-2026-10-26-002

{
  "request_key": "retry-2026-10-26-002"
}
```

*Action*: Creates a new job record with `retry_of_job_id` pointing to the previous attempt, resetting step execution while preserving previous audit events.

---

## 5. Budget Management and Accounting

Investigations enforce dual-ledger financial controls:

1. **Held Budget (`held_micros`)**: Funds reserved in advance before initiating model calls or tool executions.
2. **Spent Budget (`spent_micros`)**: Actual financial expenditure settled upon receiving and verifying model responses.
3. **Budget Limit (`budget_limit_micros`)**: The hard cap defined at investigation creation (default USD 5.00 / 5,000,000 micros).

### Handling Budget Exhaustion

If an investigation reaches its `budget_limit_micros`:
- The executor halts further model requests.
- The investigation transitions to `failed` with `failure_reason: "budget_exceeded"`.
- All unspent held budget is returned to the repository's available balance.
- To continue, the operator must fund the repository budget and trigger a new retry attempt with an adjusted budget limit.

---

## 6. Worker Daemon and Failure Recovery

Background workers execute `InvestigateSnapshotOperation` via the unified job consumer.

### Lease Mechanics and Heartbeats

- Workers lease jobs for a configured attempt duration (e.g., 90 seconds).
- The worker runtime continuously updates the lease heartbeat in PostgreSQL.
- Database clock (`CURRENT_TIMESTAMP`) is the sole authority for lease expiration.

### Automatic Recovery Daemon

The `RecoveryWorker` scans for orphan or expired jobs:
- If a worker crashes or loses network connectivity, its lease expires.
- `RecoveryWorker` detects expired leases whose deadline has passed:
  - If `attempt_count < max_attempts`: resets the job to `retry_wait` for another worker to claim.
  - If `attempt_count >= max_attempts`: marks the job as `failed` and sets investigation status to `failed`.
- Unused budget reservations for expired attempts are reclaimed automatically during reconciliation.

---

## 7. Model-Context Protocol (MCP) Read Inspection

Authorized AI agents and scripts can inspect investigation states via the read-only MCP server:

### Tool: `get_investigation_status`

```json
{
  "name": "get_investigation_status",
  "arguments": {
    "repository_id": "9f8b4a2e-4b6c-482a-a71f-0e1d8892a014",
    "investigation_id": "3c5f2b8a-9e1d-482a-a71f-2e1d8892a099"
  }
}
```

Returns bounded status JSON including status, step progress, spent/held micros, verdict, and available actions.

### Tool: `get_investigation_result`

```json
{
  "name": "get_investigation_result",
  "arguments": {
    "repository_id": "9f8b4a2e-4b6c-482a-a71f-0e1d8892a014",
    "investigation_id": "3c5f2b8a-9e1d-482a-a71f-2e1d8892a099"
  }
}
```

Returns final report payload, verified claims count, evidence count, verdict, and summary text.
