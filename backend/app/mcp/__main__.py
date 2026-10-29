"""Stdio entrypoint for CodeSPY Model Context Protocol (MCP) server.

Operator configuration is provided through environment variables:
- CODESPY_MCP_ORG_ID (required UUID)
- CODESPY_MCP_PRINCIPAL_ID (required UUID)
- CODESPY_MCP_ROLE (optional OrgRole, default: member)
- CODESPY_MCP_REPOSITORY_ALLOWLIST (required comma-separated UUIDs)
- CODESPY_DATABASE_URL (optional connection string for connected PostgreSQL mode)
- CODESPY_SOURCE_ROOT (optional path to repository retained sources)

Stdout is strictly reserved for JSON-RPC transport frames.
All diagnostic, lifecycle, and error logs are directed to stderr.
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import AsyncExitStack
from uuid import UUID

import anyio
from mcp.server.stdio import stdio_server
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.scope import OrgRole, TenantContext
from app.mcp.runtime import ConnectedMcpServer
from app.mcp.server import ReadOnlyMcpServer, create_mcp_server
from app.mcp.source_runtime import source_resources
from app.mcp.transport import bounded_stdio


def load_operator_configuration() -> tuple[TenantContext, frozenset[UUID]]:
    """Validate and load explicit operator configuration from environment."""
    raw_org = os.environ.get("CODESPY_MCP_ORG_ID")
    if not raw_org or not raw_org.strip():
        sys.stderr.write("ERROR: CODESPY_MCP_ORG_ID environment variable is required\n")
        sys.stderr.flush()
        sys.exit(1)
    try:
        org_id = UUID(raw_org.strip())
    except (ValueError, TypeError):
        sys.stderr.write("ERROR: CODESPY_MCP_ORG_ID must be a valid UUID\n")
        sys.stderr.flush()
        sys.exit(1)

    raw_principal = os.environ.get("CODESPY_MCP_PRINCIPAL_ID")
    if not raw_principal or not raw_principal.strip():
        sys.stderr.write("ERROR: CODESPY_MCP_PRINCIPAL_ID environment variable is required\n")
        sys.stderr.flush()
        sys.exit(1)
    try:
        principal_id = UUID(raw_principal.strip())
    except (ValueError, TypeError):
        sys.stderr.write("ERROR: CODESPY_MCP_PRINCIPAL_ID must be a valid UUID\n")
        sys.stderr.flush()
        sys.exit(1)

    raw_role = os.environ.get("CODESPY_MCP_ROLE", "member").strip().lower()
    try:
        role = OrgRole(raw_role)
    except ValueError:
        sys.stderr.write(f"ERROR: CODESPY_MCP_ROLE '{raw_role}' is not a valid OrgRole\n")
        sys.stderr.flush()
        sys.exit(1)

    raw_allowlist = os.environ.get("CODESPY_MCP_REPOSITORY_ALLOWLIST", "").strip()
    if not raw_allowlist:
        sys.stderr.write(
            "ERROR: CODESPY_MCP_REPOSITORY_ALLOWLIST environment variable is required\n"
        )
        sys.stderr.flush()
        sys.exit(1)

    try:
        allowed_repos = frozenset(
            UUID(part.strip()) for part in raw_allowlist.split(",") if part.strip()
        )
    except (ValueError, TypeError):
        sys.stderr.write(
            "ERROR: CODESPY_MCP_REPOSITORY_ALLOWLIST must contain valid comma-separated UUIDs\n"
        )
        sys.stderr.flush()
        sys.exit(1)

    if not allowed_repos:
        sys.stderr.write("ERROR: CODESPY_MCP_REPOSITORY_ALLOWLIST cannot be empty\n")
        sys.stderr.flush()
        sys.exit(1)

    identity = TenantContext(org_id=org_id, principal_id=principal_id, role=role)
    return identity, allowed_repos


async def run_server() -> None:
    """Initialize dependencies and run stdio MCP server."""
    identity, allowlist = load_operator_configuration()
    # SDK diagnostics may contain malformed request bodies or handler exceptions.
    # The executable emits only our explicitly redacted operational telemetry.
    sdk_logger = logging.getLogger("mcp")
    sdk_logger.handlers[:] = [logging.NullHandler()]
    sdk_logger.propagate = False

    db_url = os.environ.get("CODESPY_DATABASE_URL")
    session_factory: async_sessionmaker[AsyncSession] | None = None
    engine = None

    if db_url and db_url.strip():
        try:
            url = make_url(db_url.strip()).set(drivername="postgresql+asyncpg")
            engine = create_async_engine(
                url,
                echo=False,
                hide_parameters=True,
                isolation_level="READ COMMITTED",
                pool_size=5,
                max_overflow=0,
                pool_timeout=5,
                connect_args={
                    "timeout": 5,
                    "server_settings": {
                        "timezone": "UTC",
                        "default_transaction_read_only": "on",
                        "application_name": "codespy_mcp",
                        "statement_timeout": "10000",
                        "lock_timeout": "2000",
                        "idle_in_transaction_session_timeout": "15000",
                    },
                },
            )
            session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            sys.stderr.write("Connected PostgreSQL services initialized.\n")
        except Exception:
            if engine is not None:
                await engine.dispose()
            raise RuntimeError("MCP database startup failed") from None
    else:
        sys.stderr.write("INFO: CODESPY_DATABASE_URL not set; running in standalone mode.\n")

    sys.stderr.flush()

    from app.mcp.telemetry import McpTelemetry

    telemetry = McpTelemetry(sys.stderr)
    resources = AsyncExitStack()
    try:
        store, reader = await resources.enter_async_context(source_resources())
    except BaseException:
        if engine is not None:
            await engine.dispose()
        raise
    facade: ReadOnlyMcpServer = (
        ConnectedMcpServer(
            identity=identity,
            repository_allowlist=allowlist,
            factory=session_factory,
            store=store,
            reader=reader,
        )
        if session_factory is not None
        else ReadOnlyMcpServer(identity=identity, repository_allowlist=allowlist)
    )
    server = create_mcp_server(facade, telemetry=telemetry)

    sys.stderr.write(
        f"CodeSPY MCP server ready (org={identity.org_id}, "
        f"principal={identity.principal_id}, repositories={len(allowlist)})\n"
    )
    sys.stderr.flush()

    try:
        async with (
            bounded_stdio() as (stdin, stdout),
            stdio_server(stdin, stdout) as (read_stream, write_stream),
        ):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        try:
            await resources.aclose()
        finally:
            if engine is not None:
                await engine.dispose()
        sys.stderr.write("CodeSPY MCP server shutdown completed.\n")
        sys.stderr.flush()


def main() -> None:
    anyio.run(run_server)


if __name__ == "__main__":
    main()
