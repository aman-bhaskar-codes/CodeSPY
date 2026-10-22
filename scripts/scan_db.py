"""Explicit owned scan schema verification; never reads developer configuration."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import phase2_db as harness
from scripts.phase2_db import read_env


def test_suite(values, suite, target, selection):
    harness.verify_marker(values, values["CODESPY_TEST_DATABASE"])
    paths = {
        "migrations": ["backend/tests/phase4/storage/migrations"],
        "jobs": ["backend/tests/phase4/storage/test_scan_jobs.py"],
        "authority": [
            "backend/tests/phase4/storage/test_scan_authority.py",
            "backend/tests/phase4/storage/migrations/test_scan_guards.py",
        ],
        "publication": [
            "backend/tests/phase4/storage/test_scan_publication.py",
            "backend/tests/phase4/storage/test_scan_races.py",
        ],
        "reads": ["backend/tests/phase4/storage/test_scan_reads.py"],
        "worker": [
            "backend/tests/phase4/storage/test_scan_worker.py",
            "backend/tests/phase4/storage/test_scan_http_boundary.py",
        ],
        "native": [
            "backend/tests/phase4/storage/test_scan_native.py",
            "backend/tests/phase4/storage/test_scan_limits.py",
        ],
        "all": ["backend/tests/phase4/storage"],
    }
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AWS_")},
        **values,
        "CODESPY_TEST_SCHEMA_TARGET": target,
        "PYTHONPATH": str(harness.ROOT / "backend") + os.pathsep + str(harness.ROOT),
    }
    harness.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "backend/pyproject.toml",
            *paths[suite],
            "--import-mode=importlib",
            "--tb=short",
            *(["-k", selection] if selection else []),
        ],
        env=env,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("--env-out", required=True, type=Path)
    commands.add_parser("cleanup").add_argument("--env-file", required=True, type=Path)
    test = commands.add_parser("test")
    test.add_argument("--env-file", required=True, type=Path)
    test.add_argument(
        "--suite",
        required=True,
        choices=(
            "migrations",
            "jobs",
            "authority",
            "publication",
            "reads",
            "worker",
            "native",
            "all",
        ),
    )
    test.add_argument(
        "--schema-target",
        choices=("0012_scan_storage", "0013_scan_authority"),
        default="0013_scan_authority",
    )
    test.add_argument("--select", default="")
    args = parser.parse_args()
    if args.command == "prepare":
        harness.prepare(args.env_out, services=("postgres",))
        return
    values = read_env(args.env_file)
    if args.command == "test":
        test_suite(values, args.suite, args.schema_target, args.select)
    else:
        harness.assert_container(values)
        harness.verify_marker(values, values["CODESPY_TEST_DATABASE"])
        harness.compose(values, "down")
        args.env_file.unlink()
        print("Owned scan database removed.")


if __name__ == "__main__":
    main()
