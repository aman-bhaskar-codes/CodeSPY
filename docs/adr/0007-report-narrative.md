# ADR 0007: Report narrative as a verified synthetic investigation

Status: accepted for offline construction. Nothing in this record needs a live model; the live check is owner-run
(see the acceptance record when it exists). Builds on [ADR 0006](0006-architecture-report.md).

## Decisions

1. **One synthetic investigation per report.** The narrative is a new investigation mode (`report_narrative`) with a new
   target (`architecture_report`) that runs through the existing durable executor, journal, budget ledger and
   publication path. No parallel model path and no change to shared accounting.
2. **The narrative is pinned to one report.** The spec carries `architecture_report_id` and `architecture_report_digest`.
   Both are optional, are required exactly when the mode is `report_narrative`, and are left out of the hashed spec dump
   when `None`, so the hash of every existing spec is unchanged. (`compute_spec_hash` hashes `model_dump()` with explicit
   nulls today; any new key would otherwise change all future hashes.)
3. **Report evidence shares the evidence table.** `investigation_evidence` keeps one id space. Its source columns become
   nullable and a CHECK makes each row either fully source-bound (`observation_type='source_read'`) or fully report-bound
   (`'report_fact'`, with a `fact_ref` of report digest plus statement id). `report_fact` can never satisfy a
   `source_read` check in the verifier.
4. **Statements get computed ids.** `sid = sha256(canonical({section, code, params}))[:16]` plus an occurrence index for
   identical tuples in report order. It is not stored in the report, so `RepositoryReportV1.digest()` is unchanged, and it
   is only meaningful together with the report digest.
5. **Verification is deterministic.** A claim is confirmed only if every cited statement id was delivered to this
   investigation as a `report_fact` receipt of the pinned report, and every number, path and name in the claim text appears
   in the cited statements' text or parameters (digits, quantity words, backticked and bare names). Unknown-kind statements support only claims phrased as unknown. Unconfirmed
   sentences are shown as inference. There is no model judge.
6. **Statements, not source, go to the model.** Report parameters are paths, names and counts; they still pass through the
   model-safe transform. Instructions live in the system message only; report text is data inside a per-call random
   boundary marker; output is schema-checked and verified by code. Delimiters are a harm reducer, not the defence.
7. **Prompts are versioned assets.** They live under `backend/app/agents/prompts/<name>/<version>/`, are loaded with their
   sha256, and the hash is recorded as the spec's prompt revision. A template change without a version bump fails a test.
8. **Executor modes are explicit.** The silent `else` that treats every non-`short_path` mode as `two_specialist` is
   replaced by explicit branches. The model task name stays `synthesizer`; the closed five-name task list is not widened.

## Probed facts (PostgreSQL 16 at head `0048`)

The CHECK constraints this work widens, with their real names:
`ck_investigation_mode`, `ck_investigation_target_type` on `investigations`;
`investigation_specs_mode_check`, `investigation_specs_target_type_check` on `investigation_specs`;
`investigation_evidence_check`, `investigation_evidence_check1`, `investigation_evidence_file_sha256_check`,
`investigation_evidence_excerpt_sha256_check` on `investigation_evidence` (there is no CHECK on `observation_type`).
Migration `0049` follows the repository pattern: drop and re-add under `SET LOCAL lock_timeout`, with a downgrade that
refuses when narrative rows or `report_fact` evidence exist.

## Deferred

- A model judge for semantic support, until a qualified live runtime exists to measure it.
- A deployable worker entrypoint with Bedrock composed, and the production database role that reads
  `architecture_reports` (migration 0049 grants the test worker role only).
- Narrative quality with a real model; it is judged only in the owner-run live check.

## Changes from the plan

- No LLM synthesis call: each page is narrated independently and the narrative is the verified sentences, so
  nothing unverified (an overview) is written by the model.
- The evidence CHECK is per type rather than a closed list of observation types, so existing rows and tests with other
  type names remain valid.
- One narrative link row per published investigation report (not per report and spec), so a re-run replaces "latest".
