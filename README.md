# CodeSPY

CodeSPY is an enterprise-grade source intake, deterministic structural analysis, multi-scanner static security analysis, secure code retrieval, and reliable agentic investigation platform.

It operates under a strict principle of **evidence-grounded analysis**: zero hallucinated security findings, deterministic syntax and call-graph verification, multi-tenant database isolation, integer-microdollar budget controls, and anti-hallucination claim verification backed by exact source line quotations.

---

## Capabilities and Limits

### 1. Source Intake & Byte-Level Inventory Authority
- **Immutable Snapshots**: Tenant-isolated source archive admission with cryptographically verified SHA-256 digests.
- **Archive Ingestion Bounds**: ZIP uploads capped at 100 MiB compressed, 10,000 files, and expansion ratio limits to protect against decompression bombs.
- **Deterministic Inventory**: Comprehensive byte accounting, path sanitization, and automatic classification of source, asset, and sensitive exclusion paths.
- **Durable Job Execution**: Transactional outbox pattern, monotonic fencing leases, and resilient retry/cancellation recovery.

### 2. Structural Architecture & Framework Boundary Analysis
- **Isolated Syntax Analyzers**: Sandboxed analysis engines for Python, JavaScript, and TypeScript without executing target repository code.
- **Syntax Graph Construction**: Extraction of symbol definitions, import resolutions, call graphs, and framework boundaries (such as FastAPI, Express, and React).
- **Resource Constraints**: Default structural selection capped at 128 eligible files and 16 MiB of source, with an independent 32-file/1-MiB budget for configuration.
- **Bounded Structural Reports**: Published structural graphs are bounded at 1,000 nodes, 2,000 edges, and 2 MiB of JSON, with explicit omission reporting for untracked segments.

### 3. Static Security Analysis & Human Triage
- **Multi-Scanner Offline Adapters**: Verification pipelines for secrets detection (Gitleaks), syntactic security rules (Semgrep), software composition analysis (OSV advisory matching), and infrastructure-as-code rules.
- **Durable Findings Materialization**: Verifiable scanner occurrences mapped to exact file byte-ranges, persistent finding fingerprints, and immutable evidence envelopes.
- **Human Triage Workflow**: Audited vulnerability suppression, severity override, and state management (open, confirmed, false positive, resolved).
- **Offline Dependency Assessment**: Evaluates lockfile metadata (`uv.lock`, `package-lock.json`) against frozen offline vulnerability databases without external network egress or dependency installation.

### 4. Secure Source Retrieval & Hybrid Search
- **Multi-Modal Retrieval Engine**: Reciprocal Rank Fusion (RRF) combining dense vector embeddings (pgvector cosine distance), BM25 lexical keyword ranking, and exact AST/symbol path matching.
- **Syntax-Aware Chunking**: Bounded code chunking aligned with structural language constructs (functions, classes, interfaces).
- **Privacy Transformation**: Context-sensitive redaction and token masking to prevent secrets and sensitive identifiers from entering retrieval contexts.
- **Local Model Context Protocol (MCP) Server**: Exposes standard tools (`search_code`, `read_source_slice`, `inspect_symbol_boundary`, `list_findings`) and `codespy://` resources directly to AI engineering agents and IDEs.

### 5. Reliable Agentic Investigations & Model Orchestration
- **Dual Investigation Engines**:
  - *Default Short-Path Investigator*: Direct hypothesis validation using targeted code search, bounded file range reading, and verified repository memory inspection.
  - *Two-Specialist Adversarial Engine*: Parallel exploration by a Hypothesis Specialist (building exploitation paths) and an Adversarial Defender (identifying sanitizers and mitigations), synthesized by an Arbiter.
- **Anti-Hallucination Claim Verifier**: Every claim made by model reasoning must cite exact file paths, line ranges, and character-accurate quotes verified against disk snapshots. Unverified claims trigger automatic confidence downgrades.
- **Integer Microdollar Accounting**: Strict pre-call budget reservations and post-call settlements ($1.00 = 1,000,000 µ$). Zero model invocation proceeds without an active balance reservation.
- **Durable Leased Checkpointing**: Step-by-step state preservation with monotonic fencing tokens and append-only audit event streams.
- **Real-Time Streaming**: Server-Sent Events (SSE) channel streaming tool calls, verification results, and reasoning traces to the client.
- **Triage Memory Store**: Verified investigator insights and human triage feedback persisted to repository memory for subsequent investigation grounding.

### 6. Unified Source Desk & Security Workbench
- **Modern React Interface**: Built with React 19, TypeScript, TanStack Query, and Tailwind CSS.
- **Interactive Workbenches**:
  - *Source Desk*: Archive intake, byte inventory pagination, and ingestion job tracking.
  - *Architecture Viewer*: Framework boundaries, module relationships, and structural graphs.
  - *Findings Workbench*: Filterable multi-scanner security occurrences, evidence drawer, and triage modal.
  - *Retrieval Workbench*: Multi-channel search testing, context window assembly calculator, and repository memory manager.
  - *Investigation Workbench*: Live hypothesis submission, real-time reasoning replay, interactive claim verification cards, budget meter, and human review forms.

---

## System Limitations

- **Static Scope**: Static candidate findings and structural relationships reflect code syntax and static control flow; they do not establish runtime exploitability or complete functional safety.
- **Offline Safety**: CodeSPY never executes uploaded untrusted source code or installs uploaded dependencies.
- **Provider Spend**: Zero automated external LLM spend occurs without configured credentials and explicit budget allocations. A deterministic offline test adapter is included for testing and qualification.
- **Structural Coverage**: Byte inventory completion confirms exact physical intake; it does not imply 100% structural or type coverage across dynamic languages.

---

## Development Setup

### Prerequisites
- **Python**: 3.12+
- **Package Manager**: [uv](https://docs.astral.sh/uv/) (workspace package management)
- **Node.js**: 22+ and npm
- **Docker**: Docker Engine 24+ with Compose v2 (for database and container qualification)

### Quickstart

1. **Clone the repository**:
   ```bash
   git clone https://github.com/aman-bhaskar-codes/CodeSPY.git
   cd CodeSPY
   ```

2. **Install dependencies**:
   ```bash
   # Python workspace dependencies
   uv sync --all-packages --all-groups --frozen

   # TypeScript analyzer dependencies & build
   npm ci --prefix tools/typescript-analyzer
   npm --prefix tools/typescript-analyzer run build

   # Frontend application dependencies
   npm ci --prefix frontend
   ```

3. **Validate codebase**:
   ```bash
   # Run backend unit test suite
   uv run pytest backend/tests/phase4/unit backend/tests/phase5/unit backend/tests/phase6/unit

   # Validate frontend types, linting, and production bundle
   npm --prefix frontend run typecheck
   npm --prefix frontend run lint
   npm --prefix frontend run build
   ```

---

## Running the Application Locally

### 1. Start the Database Services
Launch the PostgreSQL (with pgvector) and local storage services:
```bash
docker compose up -d --wait
```

### 2. Prepare Local Database & Run Migrations
```bash
make database-prepare
make database-test
```

### 3. Launch Backend API Server
```bash
cd backend
PYTHONPATH=. uv run uvicorn app.api.application:create_app --factory --host 127.0.0.1 --port 8000 --reload
```

### 4. Launch Frontend Workbench
In a separate terminal:
```bash
cd frontend
npm run dev
```
Access the application at `http://127.0.0.1:5173`.

---

## Model Context Protocol (MCP) Server

CodeSPY provides an embedded Model Context Protocol (MCP) server allowing local coding assistants (such as Antigravity, Claude Desktop, or Cursor) to securely query indexed codebases and deterministic findings.

### Configuration

Add the following to your MCP client configuration (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "codespy": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/CodeSPY/backend",
        "python",
        "-m",
        "app.services.mcp_server",
        "--repository-id",
        "<REPOSITORY_UUID>",
        "--snapshot-id",
        "<SNAPSHOT_UUID>"
      ]
    }
  }
}
```

### Available MCP Tools & Resources
- **`codespy://snapshot`**: Structural metadata, file counts, and language distribution.
- **`codespy://findings`**: List of materialized security findings and severities.
- **`codespy://memory`**: Verified repository security rules and architectural notes.
- **`search_code`**: Multi-modal code search with configurable retrieval modes (hybrid, lexical, dense, exact).
- **`read_source_slice`**: Bounded, line-numbered file reader with maximum character bounds.
- **`inspect_symbol_boundary`**: Looks up syntax definitions, callers, and framework entrypoints.
- **`list_findings`**: Filters materialized security findings by scanner, severity, or rule ID.

---

## Repository Layout

```
CodeSPY/
├── backend/
│   ├── alembic/              # Database migration versions and environment
│   ├── app/
│   │   ├── api/              # FastAPI application, route handlers, and middleware
│   │   ├── contracts/        # Versioned Pydantic schemas and domain contracts
│   │   ├── db/               # Async database connection and session management
│   │   ├── domain/           # Core domain models, roles, and admission policies
│   │   ├── models/           # SQLAlchemy table definitions and schema metadata
│   │   ├── ports/            # Port interfaces for storage, auth, and model adapters
│   │   └── services/         # Domain services (intake, search, verifier, investigation)
│   ├── openapi.json          # Synchronized OpenAPI specification
│   └── tests/                # Unit, integration, evaluation, and storage test suites
├── frontend/
│   ├── src/
│   │   ├── api/              # Generated TypeScript API client bindings
│   │   └── features/
│   │       ├── findings/     # Deterministic findings workbench & triage modal
│   │       ├── investigations/# Agentic investigation workbench & replay stream
│   │       ├── retrieval/    # Hybrid search, context calculator & memory UI
│   │       └── structure/    # Structural architecture report viewer
├── infra/
│   ├── docker-compose.yml    # Local multi-service infrastructure
│   └── scanner/              # Offline containerized scanner definitions
├── tools/
│   └── typescript-analyzer/  # Node/TypeScript AST and symbol extraction tool
├── scripts/                  # Owned disposable test harnesses and database utilities
└── docs/                     # Architectural decision records, acceptance reports, specs
```

---

## Technical Records & Documentation

- **Architecture Decisions**:
  - [ADR 0001: Boundary Isolation & Setup](docs/adr/0001-setup-boundaries.md)
  - [ADR 0002: Persistence & Leased Authority](docs/adr/0002-persistence.md)
  - [ADR 0003: Structural Analysis & Sandboxing](docs/adr/0003-structural-analysis.md)
- **Acceptance & Qualification Evidence**:
  - [Persistence Qualification](docs/acceptance/persistence.md)
  - [Ingestion & Admission Qualification](docs/acceptance/ingestion.md)
  - [Structural Analysis Qualification](docs/acceptance/structural-analysis.md)
  - [Offline Scanner Integration](docs/acceptance/offline-scanner.md)
  - [Offline Dependency Assessment](docs/acceptance/offline-dependencies.md)
  - [Findings Materialization Evidence](docs/acceptance/findings-materialization.md)
  - [Durable Findings Qualification](docs/acceptance/durable-findings.md)
- **Operational Specifications**:
  - [Database Operations & Revisions](docs/database.md)
  - [Offline Scanner Qualification Research](docs/engineering/scanner-qualification.md)

---

## Contributing & Security

- **Security Disclosures**: Please consult [SECURITY.md](SECURITY.md) for vulnerability reporting procedures.
- **Contributions**: Review [CONTRIBUTING.md](CONTRIBUTING.md) for coding conventions, commit guidelines, and qualification standards.
- **License**: Distributed under the [MIT License](LICENSE).
