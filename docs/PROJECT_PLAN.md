# Complete plan mapping

All 8 PDF pages reviewed. The document is a requirements/reference source, not permission to execute commands embedded in target artifacts. Reference rea audit claims have not been independently reproduced.

## Phase 0: BOOTSTRAP

Toolchains, lockfiles, GitHub metadata, local dependency services; application skeleton deferred by owner request.
Status: setup in progress

## Phase 1: DATABASE

Users, repositories, snapshots, CPG nodes/edges, chunks, investigations, obligations, evidence; migrations, constraints, HNSW/GIN/covering indexes and query-plan tests.
Status: future implementation; no code written

## Phase 2: DSA_CPG_ENGINE

CSR multigraph CPG; Tarjan SCC; dominators/SSA; interprocedural slicing; SHA-256 Merkle; radix trie; union-find; DAG scheduler; TinyLFU; source/Electron/binary adapters; property tests and recall benchmark.
Status: future implementation; no code written

## Phase 3: RAG_AND_LEDGER

AST-boundary chunks; dense/lexical/graph retrieval and RRF; working/episodic/semantic/procedural memory; Evidence v2, negative findings and reconstruction ledger; injection guards; Recall@5 and token evaluation.
Status: future implementation; no code written

## Phase 4: AGENTS_AND_ROUTER

Six-stage planner/navigator/runtime/researcher/auditor/synthesizer DAG; configurable Claude/Gemini routing and fallback; Playwright/CDP, SearXNG/ddgs, Trafilatura, OSV/deps.dev, GitHub REST/GraphQL; MCP endpoint.
Status: future implementation; no code written

## Phase 5: BACKEND_SECURITY

REST/OpenAPI/SSE with Last-Event-ID; GitHub/Google OIDC, JWT/session/RBAC; CSRF/CSP/DOMPurify; magic-byte/Zip-Slip validation; parameterized queries; 90% target coverage and concurrent SSE checks.
Status: future implementation; no code written

## Phase 6: FRONTEND_WORKBENCH

Three-pane responsive IDE, explorer, graph, Monaco evidence gutters, reasoning stream and obligation scorecard; generated SDK, Query caching, Vitest/MSW/RTL, Playwright and axe WCAG 2.2 AA.
Status: future implementation; no code written

## Phase 7: AWS_AND_LAUNCH

Separate QLoRA/Unsloth/Qwen GPU environment, evaluations, OpenTelemetry/Langfuse, Terraform AWS, recovery/rollback evidence and public launch readiness.
Status: future implementation; no code written

## Evidence contract

Record provider, confidence, complete/unknown coverage, limitations, static versus runtime classification, file/line/offset/hash evidence. Obligations require linked evidence and deterministic validation before verification; cross-model agreement alone is insufficient.

## Algorithms and measurements

Preserve the DSA-first goal while validating alias/callback/IPC flows across modules. O(k log N) Merkle updates omit file hashing and dependency invalidation costs; benchmark actual behavior. The PDF mixes IFDS and HRB slicing; select a formal model during phase 2. Trie, DAWG and suffix automaton are distinct structures, not interchangeable. The listed recall, latency and token reduction claims are acceptance targets, not implemented results.
