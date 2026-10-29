# Local read-only MCP server

CodeSPY exposes thirteen read-only tools through the official Python MCP SDK over
stdio. The entrypoint is `python -m app.mcp`, run from the backend directory using
the repository virtual environment. macOS and Linux pipe transports are supported;
Windows transport qualification is not included.

## Authority and disclosure

The operator selects an organization, principal and repository allowlist at startup.
This is a trusted local process binding, not end-user authentication. Current database
membership, repository grants and snapshot ownership remain authoritative on each call.
The configured role is a compatibility hint, not a privilege grant.

Anyone who can launch the process with its database access is inside that local trust
boundary. Authorized source and findings returned to a host can enter that host's model
context. Configure the allowlist and host disclosure settings accordingly.

The server creates no jobs, accepts no memory reviews and invokes no model or embedding
provider. Hybrid search uses the existing retrieval service with no embedding port;
unavailable dense coverage is labelled as lexical fallback. Remote HTTP, external MCP
servers, shell commands, SQL tools and arbitrary source paths are not exposed.

## Configuration

Configuration is explicit. The entrypoint does not load a developer `.env` file or
construct cloud clients. The owner supplies database access through their environment.

| Variable | Meaning |
| --- | --- |
| `CODESPY_MCP_ORG_ID` | Required organization UUID |
| `CODESPY_MCP_PRINCIPAL_ID` | Required principal UUID |
| `CODESPY_MCP_REPOSITORY_ALLOWLIST` | Required, nonempty comma-separated repository UUIDs |
| `CODESPY_MCP_ROLE` | Optional owner/admin/member/viewer hint; defaults to member |
| `CODESPY_DATABASE_URL` | Owner-supplied PostgreSQL connection URL; absent means discovery-only mode |
| `CODESPY_SOURCE_ROOT` | Existing absolute directory containing the application's retained object store, not an uploaded source checkout |
| `CODESPY_MCP_SOURCE_IMAGE` | Qualified local source-reader image ID, `sha256:` plus 64 hexadecimal characters |
| `CODESPY_MCP_DOCKER_EXECUTABLE` | Absolute path to the Docker executable |
| `CODESPY_MCP_DOCKER_SOCKET` | Absolute path to the local Docker socket |

The image, executable and socket must be configured together with the retained-object
root. Missing source isolation produces an unavailable capability; there is no in-process
ZIP fallback. Search, context and reviewed memory can still use eligible persisted data.
Memory pointers requiring the unavailable reader are excluded by existing validation.

Connected startup checks the database and fails if it cannot connect. PostgreSQL sessions
use read-only transactions, a bounded five-connection pool, a 10-second statement timeout
and a 2-second lock timeout. Source previews compute the existing privacy transformation
without inserting derived-cache metadata. Existing contradictory or expired metadata
still fails validation.

## Codex configuration

The owner may add a `[mcp_servers.codespy]` entry to `~/.codex/config.toml` or a trusted
project's `.codex/config.toml`. No owner configuration is edited automatically. These
settings follow the [official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

Replace `/absolute/CodeSPY` with the checkout path. Forward only the variables needed
for the intended capabilities; never commit database URLs or credentials.

```toml
[mcp_servers.codespy]
command = "/absolute/CodeSPY/.venv/bin/python"
args = ["-m", "app.mcp"]
cwd = "/absolute/CodeSPY/backend"
startup_timeout_sec = 15
tool_timeout_sec = 35
env_vars = [
  "CODESPY_MCP_ORG_ID",
  "CODESPY_MCP_PRINCIPAL_ID",
  "CODESPY_MCP_REPOSITORY_ALLOWLIST",
  "CODESPY_DATABASE_URL",
  "CODESPY_SOURCE_ROOT",
  "CODESPY_MCP_SOURCE_IMAGE",
  "CODESPY_MCP_DOCKER_EXECUTABLE",
  "CODESPY_MCP_DOCKER_SOCKET",
]
```

The host must actually inherit those environment variables. Merely naming them in
`env_vars` does not provide values. Database credentials and source-reader configuration
remain owner-managed. Successful SDK tests do not prove manual Codex registration.

## Tool catalogue

| Tool | Read behavior |
| --- | --- |
| `get_index_status` | Authorized persisted index-job status |
| `get_source_view` | Verified, model-safe retained source range |
| `search_exact` | Exact symbol/path retrieval with chunk-set provenance |
| `search_hybrid` | Bounded fusion with explicit unavailable dense coverage |
| `assemble_context` | Source-context preview pinned to the selected chunk set |
| `query_memory` | Eligible reviewed memory, versions, expiry and historical hints |
| `get_investigation_status` | Authorized saved investigation status |
| `get_investigation_result` | Support-filtered report and current-support availability |
| `list_findings` | Scoped filters, pagination and current-support reasons |
| `get_evidence` | Published evidence metadata; arbitrary raw payload omitted |
| `graph_neighbors` | Bounded neighborhood from one published structural run |
| `trace_feature` | Existing finding-rooted feature trace |
| `list_claims` | Saved claims, verification and current-support availability |

Input/output schemas come from the typed catalogue. Successful calls provide structured
content and a text representation from the same validated object. Tool annotations are
hints; execution repeats authorization regardless of discovery or client caching.

## Bounds, cancellation and errors

- Incoming UTF-8 frames are capped at 64 KiB before SDK JSON parsing.
- Serialized outgoing frames are capped at 256 KiB, including protocol overhead.
- Two calls may execute and four may wait. The total deadline includes queue wait
  and is 30 seconds. Cancellation releases queue and session resources.
- Oversized collections/results return an error rather than truncated JSON or broken
  citation identifiers. Reduce requested limits or source ranges.
- Source-reader limits and existing graph/context budgets remain in force.
- EOF cancels outstanding work. A host should allow five seconds for normal exit;
  failed source-container cleanup retains its exact-owner recovery ledger.

Stderr telemetry records tool names, redacted outcomes, timing, byte counts and omissions.
It excludes arguments, queries, excerpts, memories, report bodies and exception details.
Unknown tool names are replaced with `unknown` in telemetry. Stdout is reserved for
protocol frames, including when a handler accidentally prints. Default SDK telemetry
middleware is cleared; no exporter is created automatically.

Source-reader workspaces use the `codespy-mcp-source-` temporary prefix and the existing
scanner ownership ledger. Failed cleanup must be recovered using that exact ledger and
controller; do not remove unrelated containers or delete a pending ledger blindly.

## Qualification and removal

From the checkout root, run offline transport and service-adapter checks:

```sh
.venv/bin/python -m pytest backend/tests/mcp/unit backend/tests/mcp/integration -c backend/pyproject.toml
```

Storage tests require the existing guarded disposable PostgreSQL harness and its generated
`CODESPY_TEST_*` environment. They must not target a developer or deployed database:

```sh
.venv/bin/python -m pytest backend/tests/mcp/storage -c backend/pyproject.toml
```

These tests explicitly skip without the owned environment. Their index preparation is
synthetic; it is not native indexing qualification. A separate connected source-read
case requires an explicitly configured qualified image. See the
[acceptance record](acceptance/mcp-integration.md) for observed results and outstanding gates.

For a manual host smoke test, discover the catalogue and read a known fixture's index
status, exact search, context and saved report. Revoke its grant in the owned fixture
and confirm the same running server denies subsequent reads. Record the host version,
negotiated protocol and results. No paid model call is necessary for SDK qualification.

To remove registration, remove only `[mcp_servers.codespy]` from the owner-selected TOML
configuration and reconnect the host. Confirm the child process exits. No background
scheduler or daemon is installed by this server.
