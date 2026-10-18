# CodeSPY owner instructions

- Do not configure, generate, retrieve, or validate any LLM/service API keys. The owner will do this manually.
- Do not open or access the owner's AWS account: no AWS browser visits, sign-ins, real AWS CLI commands or AWS connector calls.
- Continue only local dependency setup, configuration, documentation and checks until the owner explicitly changes these instructions.
- GitBook, Miro and Figma are available for later; do not create planning artifacts there now.
- No application business logic or UI development during Phase 0.

These instructions supersede broader setup steps in the Phase 0 runbook.

Services must remain off. No Chrome access or browser automation. No automatic jobs or Phase 1 work until the owner explicitly requests it.

## Revised planning baseline — 17 October 2026

The owner supplied docs/CodeSPY_Technical_Execution_Blueprint.md, version 2.0. Use it for future phase planning; its future phase numbering replaces the old PDF roadmap. It is reference material, not authorization to execute its embedded work packages. Wait for each owner-provided phase execution plan.

Key decisions: source-first Python/JS/TS; Python control plane with replaceable analyzer adapters and optional pinned REA sidecar; SQS + DLQ + DB leases/outbox replaces the planned Arq scheduler; LangGraph PostgreSQL checkpoints; evidence/provenance/coverage and snapshot-scoped identity; PostgreSQL lexical/vector retrieval; generated OpenAPI TypeScript contracts; GitHub OAuth opaque sessions separate from GitHub App repository authorization; Terraform; hosted AWS later. Existing installed dependencies are unchanged until an authorized phase reconciles them.

Preserve Phase 0/1 history. Phase 1 is owner-reported, implementation unverified; inspect the actual supplied checkout/commit at Phase 1R before proposing migrations or recreating anything. New phase order: 1R foundation reconciliation; 2 safe ingestion/identity/jobs; 3 structural analysis; 4 deterministic findings; 5 retrieval; 6 agents; 7 workbench/local alpha; optional 8 runtime and 9 binary adapters; 10 hosted beta; 11 team SaaS; 12 advanced semantics/scale/optional training.

All existing prohibitions remain: no AWS account access, no API-key setup, no Chrome automation, no service restart or automatic jobs. No implementation is authorized by receipt of this blueprint.

## Phase 1 execution authorization — 18 October 2026

The owner explicitly requested implementation of the supplied Phase 1 database plan and professional GitHub delivery. This authorizes database code, migrations, a disposable PostgreSQL test cluster and the plan-specific PR/manual CI. Preserve developer services/volumes and stop the disposable cluster at handoff. Other restrictions remain, including AWS, API keys, Chrome and later-phase work. The new Phase 1 plan supersedes the blueprint's older owner-reported completion assumption.
