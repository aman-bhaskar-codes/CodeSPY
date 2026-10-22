#!/usr/bin/env python3
"""Guarded, disposable local PostgreSQL test cluster. Never reads developer .env."""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import psycopg
from app.db.phase2_guard import validate_target
from dotenv import dotenv_values
from psycopg import sql
from sqlalchemy.engine import URL

PROJECT = "codespy-phase2-tests"
COMPOSE = ROOT / "compose.phase2-tests.yaml"


def run(args, *, env=None, capture=False):
    return subprocess.run(
        args, cwd=ROOT, env=env, check=True, text=True, capture_output=capture
    )


def compose(values, *args, capture=False):
    return run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "--project-name",
            PROJECT,
            "-f",
            str(COMPOSE),
            *args,
        ],
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("AWS_")},
            **values,
        },
        capture=capture,
    )


def read_env(path):
    values = {k: v for k, v in dotenv_values(path).items() if v is not None}
    validate_target(
        host=values.get("CODESPY_TEST_HOST", ""),
        port=int(values.get("CODESPY_TEST_PORT", "0")),
        database=values.get("CODESPY_TEST_DATABASE", ""),
        marker=values.get("CODESPY_TEST_MARKER", ""),
        project=values.get("CODESPY_TEST_PROJECT", ""),
    )
    return values


def assert_container(values):
    ids = compose(values, "ps", "-q", "postgres", capture=True).stdout.split()
    if len(ids) != 1:
        raise ValueError("expected one owned test container")
    info = json.loads(run(["docker", "inspect", ids[0]], capture=True).stdout)[0]
    labels = info["Config"]["Labels"]
    if (
        labels.get("com.docker.compose.project") != PROJECT
        or labels.get("codespy.phase2.marker") != values["CODESPY_TEST_MARKER"]
    ):
        raise ValueError("test container ownership mismatch")
    if info["HostConfig"].get("PortBindings", {}).get("5432/tcp") != [
        {"HostIp": "127.0.0.1", "HostPort": "55433"}
    ]:
        raise ValueError("test port binding mismatch")

    # Validate every project attachment before broad compose lifecycle operations.
    all_ids = compose(values, "ps", "-q", "--all", capture=True).stdout.split()
    for identity in all_ids:
        item = json.loads(run(["docker", "inspect", identity], capture=True).stdout)[0]
        tags = item["Config"]["Labels"]
        service = tags.get("com.docker.compose.service")
        if (
            tags.get("com.docker.compose.project") != PROJECT
            or tags.get("codespy.phase2.marker") != values["CODESPY_TEST_MARKER"]
            or service not in {"postgres", "localstack"}
        ):
            raise ValueError("test attachment ownership mismatch")
        if service == "localstack" and item["HostConfig"].get("PortBindings", {}).get(
            "4566/tcp"
        ) != [{"HostIp": "127.0.0.1", "HostPort": "4567"}]:
            raise ValueError("test emulator binding mismatch")


def connect(values, database="postgres", user="p2_admin", password=None):
    return psycopg.connect(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        dbname=database,
        user=user,
        password=password or values["CODESPY_TEST_ADMIN_PASSWORD"],
        autocommit=True,
        connect_timeout=5,
    )


def url(values, database, user, password, driver="postgresql+psycopg"):
    return URL.create(
        driver,
        username=user,
        password=password,
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        database=database,
    )


def provision_extensions(values, database):
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        database=database,
        marker=values["CODESPY_TEST_MARKER"],
        project=PROJECT,
    )
    with connect(values, database) as c:
        for ext in ["uuid-ossp", "citext", "pg_trgm", "vector"]:
            c.execute(
                sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(sql.Identifier(ext))
            )
        return dict(c.execute("SELECT extname,extversion FROM pg_extension").fetchall())


def provision(values, database):
    provision_extensions(values, database)
    with connect(values, database) as c:
        c.execute("CREATE SCHEMA codespy_test_guard")
        c.execute("CREATE TABLE codespy_test_guard.ownership(marker text NOT NULL)")
        c.execute(
            "INSERT INTO codespy_test_guard.ownership VALUES (%s)",
            (values["CODESPY_TEST_MARKER"],),
        )
        c.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        c.execute("GRANT USAGE, CREATE ON SCHEMA public TO p2_migration")
        c.execute("GRANT USAGE ON SCHEMA public TO p2_api,p2_broker")


def verify_marker(values, database):
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        database=database,
        marker=values["CODESPY_TEST_MARKER"],
        project=PROJECT,
    )
    assert_container(values)
    with connect(values, database) as c:
        marker = c.execute("SELECT marker FROM codespy_test_guard.ownership").fetchall()
        if marker != [(values["CODESPY_TEST_MARKER"],)]:
            raise ValueError("database ownership mismatch")


def create_database(values, suffix="suite"):
    assert_container(values)
    name = "codespy_p2_test_" + suffix + "_" + uuid4().hex[:12]
    validate_target(
        host=values["CODESPY_TEST_HOST"],
        port=int(values["CODESPY_TEST_PORT"]),
        database=name,
        marker=values["CODESPY_TEST_MARKER"],
        project=PROJECT,
    )
    with connect(values) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} OWNER p2_migration TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    provision(values, name)
    return name


def drop_database(values, name):
    verify_marker(values, name)
    with connect(values) as c:
        c.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
        if c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            raise RuntimeError("database cleanup failed")


def migrate(values, name, target="0009_api_commands", downgrade=False):
    from alembic import command
    from alembic.config import Config

    config = Config(str(ROOT / "backend/alembic.ini"))
    config.attributes["connection_url"] = url(
        values, name, "p2_migration", values["CODESPY_TEST_MIGRATION_PASSWORD"]
    )
    # env.py deliberately receives an explicit DB-only settings destination.
    prior = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = config.attributes["connection_url"].render_as_string(
        hide_password=False
    )
    try:
        (command.downgrade if downgrade else command.upgrade)(config, target)
    finally:
        if prior is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prior


def grant_app(values, name):
    with connect(values, name) as c:
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO p2_api,p2_broker")
        c.execute(
            "GRANT EXECUTE ON FUNCTION public.codespy_phase2_authority() TO p2_broker"
        )
        workflow = "external_identities,oauth_flows,sessions,github_user_grants,repository_grants,github_repository_bindings,org_work_limits,object_records,source_submissions,cleanup_tasks,jobs,job_attempts,outbox,job_events,webhook_receipts"
        c.execute("GRANT INSERT,UPDATE ON " + workflow + " TO p2_api,p2_broker")
        c.execute(
            "GRANT INSERT ON organizations,principals,memberships,repositories,api_commands TO p2_api"
        )
        # PostgreSQL row locks require UPDATE on at least one column.
        c.execute("GRANT UPDATE(role) ON memberships TO p2_api,p2_broker")
        c.execute("GRANT UPDATE(display_name) ON repositories TO p2_api,p2_broker")
        c.execute(
            "GRANT INSERT ON artifacts,source_snapshots,source_files,inventory_entries,analysis_runs,cpg_nodes,cpg_edges,evidence_records,evidence_derivations TO p2_broker"
        )
        c.execute("GRANT UPDATE ON source_snapshots,analysis_runs TO p2_broker")
        c.execute(
            "GRANT DELETE ON job_events,oauth_flows,sessions,webhook_receipts TO p2_broker"
        )
        if c.execute("SELECT to_regclass('public.analysis_specs')").fetchone()[0]:
            c.execute("GRANT INSERT ON analysis_specs TO p2_api")
            c.execute("GRANT INSERT ON structural_reports TO p2_broker")
        if c.execute("SELECT to_regclass('public.scan_specs')").fetchone()[0]:
            c.execute("GRANT INSERT ON scan_specs TO p2_api")
            c.execute(
                "GRANT INSERT ON scan_attempt_specs,scan_reports,scan_observations,"
                "finding_occurrences,finding_support,verification_decisions TO p2_broker"
            )
            if c.execute(
                "SELECT to_regprocedure('public.codespy_scan_complete_set(uuid,uuid,uuid)')"
            ).fetchone()[0]:
                c.execute(
                    "GRANT EXECUTE ON FUNCTION public.codespy_scan_complete_set(uuid,uuid,uuid), "
                    "public.codespy_scan_json(jsonb),public.codespy_scan_digest(jsonb) TO p2_broker"
                )
        if c.execute("SELECT to_regclass('public.finding_triage_events')").fetchone()[
            0
        ]:
            c.execute("GRANT INSERT ON finding_triage_events TO p2_api")
            c.execute("GRANT INSERT, UPDATE ON finding_triage_projections TO p2_api")
        c.execute("REVOKE ALL ON public.alembic_version FROM p2_api,p2_broker")
        c.execute("REVOKE CREATE ON SCHEMA public FROM p2_api,p2_broker")


def prepare(path, *, services=("postgres", "localstack")):
    if (
        not services
        or len(set(services)) != len(services)
        or not set(services) <= {"postgres", "localstack"}
        or "postgres" not in services
    ):
        raise ValueError("invalid owned test service selection")
    if path.exists():
        raise ValueError("refusing to overwrite existing ownership file")
    values = {
        "CODESPY_TEST_HOST": "127.0.0.1",
        "CODESPY_TEST_PORT": "55433",
        "CODESPY_TEST_PROJECT": PROJECT,
        "CODESPY_TEST_MARKER": secrets.token_hex(16),
        "CODESPY_TEST_DATABASE": "codespy_p2_test_" + uuid4().hex[:12],
        "CODESPY_TEST_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CODESPY_TEST_MIGRATION_PASSWORD": secrets.token_urlsafe(24),
        "CODESPY_TEST_APP_PASSWORD": secrets.token_urlsafe(24),
        "CODESPY_TEST_BROKER_PASSWORD": secrets.token_urlsafe(24),
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Manifest precedes service side effects, retained on any partial failure.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={v}\n" for k, v in values.items()))
    # Refuse adopting an already-running project with a different marker.
    existing = compose(values, "ps", "-q", "--all", capture=True).stdout.strip()
    if existing:
        assert_container(values)
    compose(values, "up", "-d", "--wait", "--wait-timeout", "120", *services)
    assert_container(values)
    with connect(values) as c:
        for role, key in [
            ("p2_migration", "CODESPY_TEST_MIGRATION_PASSWORD"),
            ("p2_api", "CODESPY_TEST_APP_PASSWORD"),
            ("p2_broker", "CODESPY_TEST_BROKER_PASSWORD"),
        ]:
            c.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
                ).format(sql.Identifier(role), sql.Literal(values[key]))
            )
        c.execute(
            sql.SQL("CREATE DATABASE {} OWNER p2_migration TEMPLATE template0").format(
                sql.Identifier(values["CODESPY_TEST_DATABASE"])
            )
        )
    provision(values, values["CODESPY_TEST_DATABASE"])
    print(
        "Disposable PostgreSQL prepared; credentials retained only in ignored owner file."
    )


def test_suite(values, suite, schema_target="0009_api_commands"):
    verify_marker(values, values["CODESPY_TEST_DATABASE"])
    name = create_database(values, suite)
    try:
        migrate(values, name, schema_target)
        grant_app(values, name)
        env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("AWS_")},
            **values,
            "CODESPY_TEST_DATABASE": name,
            "CODESPY_TEST_SCHEMA_TARGET": schema_target,
            "PYTHONPATH": str(ROOT / "backend") + os.pathsep + str(ROOT),
        }
        paths = {
            "phase3": ["backend/tests/phase3"],
            "phase4": ["backend/tests/phase4"],
            "integration": [
                "backend/tests/phase2/integration",
                "backend/tests/phase2/api",
            ],
            "migrations": ["backend/tests/phase2/migrations"],
            "concurrency": ["backend/tests/phase2/integration"],
            "performance": ["backend/tests/phase2/performance"],
            "emulator": ["backend/tests/phase2/emulator"],
            "all": [
                "backend/tests/phase2/integration",
                "backend/tests/phase2/api",
                "backend/tests/phase2/migrations",
                "backend/tests/phase2/emulator",
                "backend/tests/phase2/unit",
                "backend/tests/phase2/ports",
                "backend/tests/phase2/admission",
            ],
            "migration": ["backend/tests/phase2/migrations"],
            "api": ["backend/tests/phase2/api"],
            "ports": ["backend/tests/phase2/ports"],
            "admission": ["backend/tests/phase2/admission"],
            "jobs": ["backend/tests/phase2/integration"],
            "github": [
                "backend/tests/phase2/unit/test_github_http.py",
                "backend/tests/phase2/unit/test_github_security.py",
                "backend/tests/phase2/unit/test_github_transport.py",
                "backend/tests/phase2/integration/test_github_acquisition.py",
                "backend/tests/phase2/integration/test_github_binding.py",
            ],
            "storage": ["backend/tests/phase2/ports", "backend/tests/phase2/emulator"],
            "database": ["backend/tests/phase2/integration"],
            "cleanup": ["backend/tests/phase2/integration/test_cleanup.py"],
        }
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                "backend/pyproject.toml",
                *paths[suite],
                *(
                    ["-k", os.environ["CODESPY_TEST_SELECT"]]
                    if os.environ.get("CODESPY_TEST_SELECT")
                    else []
                ),
                *(["-s"] if suite in {"performance", "phase3"} else []),
                "-o",
                "addopts=-ra --strict-markers",
                "-q",
            ],
            env=env,
        )
    finally:
        drop_database(values, name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--env-out", type=Path, required=True)
    for command in ["cleanup", "test", "provision-extensions"]:
        p = sub.add_parser(command)
        p.add_argument("--env-file", type=Path, required=True)
        if command == "test":
            p.add_argument(
                "--schema-target",
                default="0009_api_commands",
                choices=[
                    "0009_api_commands",
                    "0011_structural_authority",
                    "0012_scan_storage",
                ],
            )
            p.add_argument(
                "--suite",
                choices=[
                    "phase3",
                    "phase4",
                    "integration",
                    "migrations",
                    "concurrency",
                    "performance",
                    "emulator",
                    "all",
                    "migration",
                    "api",
                    "ports",
                    "admission",
                    "jobs",
                    "github",
                    "storage",
                    "database",
                    "cleanup",
                ],
                required=True,
            )
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.env_out)
        return
    values = read_env(args.env_file)
    if args.command == "cleanup":
        assert_container(values)
        compose(values, "down")
        args.env_file.unlink()
        print("Owned disposable cluster removed; developer volumes untouched.")
    elif args.command == "test":
        verify_marker(values, values["CODESPY_TEST_DATABASE"])
        test_suite(values, args.suite, args.schema_target)
    else:
        verify_marker(values, values["CODESPY_TEST_DATABASE"])
        provision_extensions(values, values["CODESPY_TEST_DATABASE"])
        with connect(values, values["CODESPY_TEST_DATABASE"]) as c:
            print("PostgreSQL", c.execute("SHOW server_version").fetchone()[0])
            print(
                "Extensions:",
                c.execute(
                    "SELECT extname,extversion FROM pg_extension ORDER BY extname"
                ).fetchall(),
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, psycopg.Error):
        print(
            "Disposable database operation rejected; inspect destination and ownership configuration.",
            file=sys.stderr,
        )
        sys.exit(1)
