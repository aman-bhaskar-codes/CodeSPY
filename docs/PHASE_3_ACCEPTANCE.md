# Phase 3 local acceptance

The local implementation covers deterministic structural analysis, code property graph extraction, symbol resolution, cross-boundary route/client matching, report materialization, and React UI visualization. This document records local qualification; cloud deployment and hosted live gates remain separate.

## Implemented boundaries

- **Toolchain qualification (WP-3.00)**: Pinned Tree-sitter 0.26.0 with native Python, JavaScript, and TS/TSX grammars, plus isolated TypeScript 5.9.3 analyzer sidecar. Demonstrated bounded external deadlines and cancellation; avoided native progress callback segmentation faults.
- **Contracts and DB schema (WP-3.01)**: Additive Alembic revisions `0010_structural_specs` and `0011_structural_authority` adding `analysis_specs` and `structural_reports` tables (bringing catalog to 31 tables). Enforced database authority triggers restricting report insertion to authorized worker leases.
- **Position and source reader (WP-3.03)**: Exact UTF-8 byte and UTF-16 code unit index conversion (`positions.py`), deterministic identity projections (`identities.py`), and quarantined `AdmittedSourceReader` verifying archive/manifest digests under strict admission policies.
- **Language syntax extractors (WP-3.04)**: Deterministic Tree-sitter AST queries extracting definitions, calls, imports, and exports across Python, JS/JSX, and TS/TSX dialects.
- **Python resolution (WP-3.05)**: Lexical scope tracking, function/class symbol tables, relative/absolute intra-snapshot import resolution without runtime execution.
- **TypeScript sidecar (WP-3.06)**: Standalone Node CLI communicating over bounded stdin/stdout JSON protocol with virtual compiler host, custom module resolution, and declaration mapping.
- **Pure graph algorithms (WP-3.07)**: Deterministic Strongly Connected Components (Tarjan/SCC), condensation, cycle detection, depth-bounded traversal, and scope projection.
- **Framework boundaries (WP-3.08)**: Static route observers for FastAPI and Express, HTTP client call observers, and cross-service boundary candidate matching via explicit service bindings.
- **Integrated pipeline (WP-3.09)**: End-to-end `analyze()` pipeline with coverage reconciliation, excerpt digests, and canonical JSON/Markdown rendering.
- **Job lifecycle & publication (WP-3.10)**: Fenced worker operation `analyze_structure`, `PublicationExecutor` with transaction timeout and connection termination, and `StructuralFinalizer` atomic publication.
- **API endpoints (WP-3.11)**: `POST /repositories/{id}/analyses`, `GET /analyses/{run_id}/report`, `GET /analyses/{run_id}/export`, and `GET /snapshots/{id}/structural-report` with OpenAPI contracts.
- **Frontend UI (WP-3.12)**: `StructuralReportView` React component, TanStack Query integration, export downloads, accessible tables, and status announcements.

## Executed verification suite on 19 October 2026

| Check | Result |
| :--- | :--- |
| Phase 3 unit test suite (`backend/tests/phase3/unit`) | 172 passed in 1.46 s |
| Phase 2 unit & ports test suite | 28 passed in 0.79 s |
| TypeScript analyzer sidecar tests | 10 passed in 0.63 s |
| Strict mypy typing (`backend/app`) | 0 issues across 107 source files |
| Ruff linting (`backend/app`, `backend/tests/phase3`) | All checks passed (0 errors) |
| Frontend unit tests (`frontend/src/App.test.tsx`) | 6 passed in 1.08 s |
| Frontend typecheck (`tsc --noEmit`) | 0 issues |
| Frontend linting (`eslint . --max-warnings 0`) | 0 warnings, 0 errors |
| Frontend production build (`tsc -b && vite build`) | Built in 117 ms |

## Nonclaims and constraints

- No dynamic code execution, eval, or runtime import was performed on target sources.
- No LLM, RAG, or heuristic guesswork was used in structural analysis; all results derive from deterministic AST and lexical analysis.
- No AWS account access, real API key configuration, or hosted cloud resources were used.
- Staged developer files and research notes were preserved intact throughout the implementation.
