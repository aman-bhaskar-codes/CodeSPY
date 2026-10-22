"""Read-only dependency setup acceptance checks; no application implementation."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


def command(root: Path, *args: str) -> str:
    result = subprocess.run(
        args, cwd=root, capture_output=True, text=True, timeout=60, check=True
    )
    return result.stdout.strip()


def require(condition: object, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def static_checks(root: Path, check: Callable[..., None]) -> None:
    paths = [
        "LICENSE",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "frontend/package.json",
        "frontend/package-lock.json",
        "uv.lock",
        "docker-compose.deps.yml",
        "docker/postgres/init-extensions.sql",
        "docker/searxng/settings.yml",
        ".env.example",
        ".pre-commit-config.yaml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
    ]
    for name in paths:
        check(
            "file: " + name,
            lambda name=name: require(
                (root / name).is_file(), "Required file is missing"
            ),
        )
    folders = [
        "backend/app/api/v1",
        "backend/app/core/dsa",
        "backend/app/agents",
        "backend/app/analyzers",
        "backend/app/rag",
        "backend/app/models",
        "backend/app/schemas",
        "backend/app/repositories",
        "backend/app/services",
        "backend/app/workers",
        "backend/alembic/versions",
        "backend/tests/unit",
        "backend/tests/integration",
        "backend/tests/benchmarks",
        "frontend/public",
        "frontend/src/api",
        "frontend/src/components",
        "frontend/src/features",
        "frontend/src/hooks",
        "frontend/src/lib",
        "frontend/src/pages",
        "frontend/src/styles",
        "frontend/tests/e2e",
        "infra/terraform/environments/dev",
        "infra/terraform/environments/prod",
        "infra/terraform/modules",
        "docs/acceptance",
        "docs/architecture",
    ]
    for name in folders:
        check(
            "directory: " + name,
            lambda name=name: require(
                (root / name).is_dir(), "Required directory is missing"
            ),
        )


def local_checks(root: Path, check: Callable[..., None]) -> None:
    check(
        "Python 3.12",
        lambda: require(
            sys.version_info[:2] == (3, 12), "Use the project Python 3.12 environment"
        ),
    )
    check(
        "Node 22",
        lambda: require(
            command(root, "node", "--version").startswith("v22."),
            "Source scripts/env.sh to select Node 22",
        ),
    )
    for tool in [
        "git",
        "uv",
        "npm",
        "docker",
        "aws",
        "terraform",
        "gitleaks",
        "gh",
        "semgrep",
        "analyzeHeadless",
        "rizin",
        "jadx",
        "wasm2wat",
        "binwalk",
        "unblob",
        "asar",
    ]:
        check(
            "binary: " + tool,
            lambda tool=tool: require(
                shutil.which(tool), "Tool is not on the project PATH"
            ),
        )
    check(
        "JDK 21",
        lambda: require(
            'version "21'
            in subprocess.run(
                ["java", "-version"], capture_output=True, text=True, check=True
            ).stderr,
            "JDK 21 is not selected",
        ),
    )
    modules = [
        "fastapi",
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "alembic",
        "pgvector",
        "redis",
        "arq",
        "boto3",
        "aioboto3",
        "anthropic",
        "google.genai",
        "mcp",
        "tiktoken",
        "tree_sitter",
        "tree_sitter_language_pack",
        "networkx",
        "numpy",
        "pygit2",
        "git",
        "playwright",
        "ddgs",
        "duckduckgo_search",
        "trafilatura",
        "bs4",
        "jwt",
        "pwdlib",
        "authlib",
        "magic",
        "structlog",
        "opentelemetry.instrumentation.fastapi",
        "opentelemetry.instrumentation.sqlalchemy",
        "opentelemetry.instrumentation.httpx",
        "langfuse",
    ]
    for name in modules:
        check("import: " + name, lambda name=name: importlib.import_module(name))
    check(
        "frontend dependency tree",
        lambda: command(root, "npm", "--prefix", "frontend", "ls", "--depth=0"),
    )
    service_checks(root, check)

    def browser() -> None:
        from dotenv import dotenv_values
        from playwright.sync_api import sync_playwright

        values = dotenv_values(root / ".env")
        browser_path = values.get("PLAYWRIGHT_BROWSERS_PATH")
        if browser_path:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browser_path
        with sync_playwright() as engine:
            session = engine.chromium.launch()
            try:
                page = session.new_page()
                page.goto("data:text/html,<title>CodeSPY setup check</title>")
                require(
                    page.title() == "CodeSPY setup check", "Browser smoke check failed"
                )
            finally:
                session.close()

    check("Chromium smoke check", browser)


def service_checks(root: Path, check: Callable[..., None]) -> None:
    check(
        "Compose config",
        lambda: command(root, "docker", "compose", "config", "--quiet"),
    )

    def services() -> None:
        rows = [
            json.loads(line)
            for line in command(
                root, "docker", "compose", "ps", "--format", "json"
            ).splitlines()
            if line
        ]
        states = {row["Service"]: row for row in rows}
        for service in ["postgres", "redis", "searxng", "localstack"]:
            require(
                service in states and states[service]["State"] == "running",
                "A required service is not running: " + service,
            )
        for service in ["postgres", "redis", "localstack"]:
            require(
                states[service].get("Health") == "healthy",
                "Healthcheck not passing: " + service,
            )

    check("four running dependency services", services)

    def extensions() -> None:
        names = set(
            command(
                root,
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "psql",
                "-U",
                "codespy",
                "-d",
                "codespy",
                "-Atc",
                "SELECT extname FROM pg_extension;",
            ).splitlines()
        )
        require(
            {"vector", "pg_trgm", "citext", "uuid-ossp"} <= names,
            "Database extensions are incomplete",
        )

    check("PostgreSQL extensions", extensions)
    check(
        "authenticated Redis PING",
        lambda: require(
            command(
                root,
                "docker",
                "compose",
                "exec",
                "-T",
                "redis",
                "sh",
                "-c",
                'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping',
            )
            == "PONG",
            "Redis PING failed",
        ),
    )

    def search() -> None:
        import httpx

        response = httpx.get(
            "http://127.0.0.1:18080/config", timeout=15, trust_env=False
        )
        response.raise_for_status()

    check("SearXNG HTTP endpoint", search)

    def emulator() -> None:
        import boto3
        from botocore.config import Config

        settings = {
            "config": Config(proxies={}, ignore_configured_endpoint_urls=True),
            "endpoint_url": "http://127.0.0.1:4566",
            "region_name": "ap-south-1",
            "aws_access_key_id": "test",
            "aws_secret_access_key": "test",
        }
        s3 = boto3.client("s3", **settings)
        require(
            any(
                b["Name"] == "codespy-artifacts-dev"
                for b in s3.list_buckets()["Buckets"]
            ),
            "Local artifact bucket is missing; run bootstrap_local.sh",
        )
        sqs = boto3.client("sqs", **settings)
        sqs.get_queue_url(QueueName="codespy-analysis-jobs")
        sqs.get_queue_url(QueueName="codespy-analysis-dlq")
        boto3.client("secretsmanager", **settings).describe_secret(
            SecretId="codespy/dev/api-keys"
        )

    check("LocalStack S3/SQS/secrets metadata", emulator)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scope",
        choices=["local", "services", "static"],
        default="local",
    )
    parser.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    failures: list[str] = []
    passed: list[str] = []

    def check(name: str, action: Callable[[], object]) -> None:
        try:
            action()
            passed.append(name)
            print("PASS " + name)
        except Exception as error:  # noqa: BLE001 -- acceptance runner must report every failed check
            failures.append(name)
            print(
                "FAIL " + name + ": " + type(error).__name__ + ": " + str(error)[:180]
            )

    if args.scope in ["local", "static"]:
        static_checks(args.project_root, check)
    if args.scope == "local":
        local_checks(args.project_root, check)
    if args.scope == "services":
        service_checks(args.project_root, check)
    print(
        json.dumps(
            {
                "scope": args.scope,
                "passed": len(passed),
                "failed": len(failures),
                "missing": failures,
                "local_setup_complete": args.scope == "local" and not failures,
                "owner_managed": ["API keys", "AWS account access"],
            }
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
