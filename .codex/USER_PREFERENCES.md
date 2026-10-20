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

## Phase 2 execution authorization — 18 October 2026

The owner explicitly answered “Revise the plan, then implement Phase 2.” This authorizes the revised Phase 2 application work, additive migrations and owned disposable local tests described in its plan. It does not authorize AWS account access, real service/API-key setup, browser automation, deployment or merging. Stop owned test resources at handoff and preserve developer services, files and volumes.

## Phase 2 delivery authorization — 19 October 2026

The owner explicitly requested completing Phase 2, fixing GitHub pipelines and pushing the verified result to main. This supersedes the earlier prohibition on merging for this delivery. Normal branch protection and all AWS, real-key, browser and deployment restrictions remain.

## Phase 3 toolchain authorization — 19 October 2026

The owner transferred development back to this agent, requested REA-assisted investigation and approved WP-3.00 of the complete structural-analysis execution plan. This authorizes exact trusted parser/compiler pins, fixtures, qualification documents and owned disposable Linux toolchain probes. REA 6.7.0 may be used as an investigation tool; it does not become a production authority or runtime dependency. Later structural packages follow the plan and accepted decisions. Preserve owner-staged duplicate files and the owner research document. AWS account access, real-key work, deployment and browser-session automation remain restricted.

## Phase 3 completion and delivery authorization — 19 October 2026

The owner explicitly requested completing Phase 3 step by step, updating README and related records, and integrating all verified commits into main at the end. This authorizes the complete approved structural execution plan, owned local verification, GitHub PR/CI and normal protected merging. It does not authorize AWS, real-key work, browser automation or deployment. Preserve owner-staged duplicates.

## Post-Phase-3 execution authorization — 20 October 2026

The owner approved the post-Phase-3 master roadmap and requested step-by-step development. Use docs/CODESPY_POST_PHASE3_MASTER_EXECUTION_ROADMAP.md as the controlling future roadmap and begin P4.00 against repaired Phase 3. Development and public Chrome research are authorized; use fresh contexts for product tests. The owner approved an isolated Phase 4 worktree. Preserve historical migrations and owner files. Ask before sensitive actions, including real credential access, AWS account/resource changes, paid calls, deployment and consequential external changes. API keys remain owner-filled. No new service should remain running at handoff. Older Phase 0 restrictions on application development and public browser research are superseded by this instruction; they are not permission to access AWS sessions or credentials.
