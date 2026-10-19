"""Build an owned minimal admission image from a whitelist, never the repository context."""

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
    "admission/entrypoint.py",
    "admission/zip_inventory.py",
    "security/archive_paths.py",
    "contracts/ingestion.py",
    "domain/scope.py",
    "domain/canonical_json.py",
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
    (ROOT / "infra/admission/requirements.txt").write_text(requirements)
    with tempfile.TemporaryDirectory(
        prefix="admission-build-", dir=ROOT / "work"
    ) as directory:
        target = Path(directory)
        shutil.copyfile(ROOT / "infra/admission/Dockerfile", target / "Dockerfile")
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
                "codespy.admission.source_hash=" + digest.hexdigest(),
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
