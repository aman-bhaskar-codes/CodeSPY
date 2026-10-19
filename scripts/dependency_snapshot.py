"""Describe the root uv workspace lock using GitHub's dependency snapshot format."""

import argparse
import json
import os
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote


def snapshot(root: Path, env: dict[str, str]) -> dict:
    lock = tomllib.loads((root / "uv.lock").read_text())
    resolved = {}
    for package in lock["package"]:
        # Workspace members are source, not acquired third-party dependencies.
        if "registry" not in package.get("source", {}):
            continue
        name = package["name"].lower().replace("_", "-").replace(".", "-")
        purl = f"pkg:pypi/{quote(name, safe='-')}@{quote(package['version'], safe='')}"
        resolved[purl] = {"package_url": purl}
    return {
        "version": 0,
        "sha": env["GITHUB_SHA"],
        "ref": env["GITHUB_REF"],
        "job": {
            "id": env["GITHUB_RUN_ID"],
            "correlator": "codespy-root-uv-lock",
        },
        "detector": {
            "name": "codespy-uv-lock",
            "version": "1.0.0",
            "url": "https://github.com/aman-bhaskar-codes/CodeSPY",
        },
        "scanned": datetime.now(UTC).isoformat(),
        "manifests": {
            "uv.lock": {
                "name": "uv.lock",
                "file": {"source_location": "uv.lock"},
                "resolved": resolved,
            }
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(snapshot(Path.cwd(), dict(os.environ)), indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
