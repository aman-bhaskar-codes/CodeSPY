# Scan status

Both scan creation routes return an accepted response containing `spec_id`, `job_id`, `snapshot_id` and `status_url`. Follow `status_url` to read the durable job before a report exists:

```text
GET /api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/scan-jobs/{job_id}
```

The response uses `ScanJobRecordV4`, including `state`, `attempt_count`, `failure_code`, `result_run_id` and `report_available`. The job ID identifies the durable request. A result run ID identifies a published analysis attempt; clients must not substitute the job ID into report URLs.

After a successful or partial publication, use the returned `result_run_id` with the existing summary route:

```text
GET /api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/scans/{run_id}/summary
```

A session and current repository read permission are required on every status read. Organization, repository, snapshot and scan operation must match. Foreign or unavailable jobs return 404. Scanner capabilities need not be enabled to read existing jobs. The legacy job routes retain their existing scan-operation rejection; this endpoint does not add scan cancellation, retry or event streaming.

Status tracking does not enable a scanner runtime. Scan creation still requires explicitly injected scan capabilities and a valid configuration. Native execution and PostgreSQL publication qualification remain separate from authenticated HTTP transport tests.
