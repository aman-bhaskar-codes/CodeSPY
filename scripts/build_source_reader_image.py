"""Build an owned minimal source reader image from a whitelist, never the repository context."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "source_read/entrypoint.py",
    "source_read/extract.py",
    "ports/source_reader.py",
    "admission/zip_inventory.py",
    "security/archive_paths.py",
    "contracts/ingestion.py",
    "domain/scope.py",
    "domain/canonical_json.py",
    "domain/errors.py",
]
PACKAGES = {
    "pydantic",
    "pydantic-core",
    "annotated-types",
    "typing-extensions",
    "typing-inspection",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-out", type=Path, required=True)
    args = parser.parse_args()
    if args.image_out.exists() or args.image_out.is_symlink():
        raise ValueError("new owned image output required")
    args.image_out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    lines = []
    for package in lock["package"]:
        if package["name"] not in PACKAGES:
            continue
        hashes = sorted({w["hash"] for w in package.get("wheels", [])})
        if not hashes:
            raise ValueError("locked binary dependencies required")
        lines.append(
            package["name"]
            + "=="
            + package["version"]
            + " "
            + " ".join("--hash=" + h for h in hashes)
        )
    if len(lines) != len(PACKAGES):
        raise ValueError("incomplete admission dependency closure")
    requirements = "\n".join(sorted(lines)) + "\n"
    with tempfile.TemporaryDirectory(
        prefix="source-reader-build-", dir=ROOT / "work"
    ) as directory:
        target = Path(directory)
        shutil.copyfile(ROOT / "infra/source-reader/Dockerfile", target / "Dockerfile")
        (target / "requirements.txt").write_text(requirements)
        digest = hashlib.sha256(requirements.encode())
        for file in FILES:
            destination = target / "app" / file
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = ROOT / "backend/app" / file
            shutil.copyfile(source, destination)
            digest.update(file.encode())
            digest.update(source.read_bytes())
        subprocess.run(
            [
                "docker",
                "build",
                "--label",
                "codespy.source-reader.source-hash=" + digest.hexdigest(),
                "--iidfile",
                str(args.image_out.absolute()),
                str(target),
            ],
            check=True,
            cwd=ROOT,
        )
    args.image_out.chmod(0o600)
    print(
        json.dumps(
            {
                "image_id": args.image_out.read_text().strip(),
                "source_hash": digest.hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
