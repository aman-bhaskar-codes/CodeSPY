"""Build trusted qualification fixtures only, from a small temporary context."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "tree-sitter",
    "tree-sitter-python",
    "tree-sitter-javascript",
    "tree-sitter-typescript",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-out", type=Path, required=True)
    args = parser.parse_args()
    if args.image_out.exists() or args.image_out.is_symlink():
        raise ValueError("choose a new owned image output file")
    args.image_out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    requirements = []
    for package in lock["package"]:
        if package["name"] in PACKAGES:
            hashes = sorted({wheel["hash"] for wheel in package["wheels"]})
            if not hashes:
                raise ValueError("binary wheel hashes required")
            requirements.append(
                package["name"]
                + "=="
                + package["version"]
                + " "
                + " ".join("--hash=" + h for h in hashes)
            )
    if len(requirements) != len(PACKAGES):
        raise ValueError("incomplete trusted parser closure")
    files = {
        "infra/analysis/qualification/Dockerfile": "Dockerfile",
        "infra/analysis/qualification/probe.py": "probe.py",
        "infra/analysis/qualification/progress_probe.py": "progress_probe.py",
        "tools/typescript-analyzer/package.json": "typescript/package.json",
        "tools/typescript-analyzer/package-lock.json": "typescript/package-lock.json",
        "tools/typescript-analyzer/tests/capabilities.test.cjs": "typescript/tests/capabilities.test.cjs",
    }
    with tempfile.TemporaryDirectory(
        prefix="phase3-build-", dir=args.image_out.parent
    ) as folder:
        context = Path(folder)
        (context / "requirements.txt").write_text(
            "\n".join(sorted(requirements)) + "\n"
        )
        for source, destination in files.items():
            path = context / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, path)
        digest = hashlib.sha256()
        for path in sorted(context.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(context).as_posix().encode())
                digest.update(path.read_bytes())
        subprocess.run(
            [
                "docker",
                "build",
                "--iidfile",
                str(args.image_out.absolute()),
                "--label",
                "codespy.phase3.toolchain-sha256=" + digest.hexdigest(),
                str(context),
            ],
            check=True,
        )
    args.image_out.chmod(0o600)
    print(
        json.dumps(
            {
                "image": args.image_out.read_text().strip(),
                "context_sha256": digest.hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
