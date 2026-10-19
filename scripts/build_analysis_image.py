"""Build only trusted analyzer sources and exact hash-locked binary dependencies."""

import argparse
import hashlib
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
    "pydantic",
    "pydantic-core",
    "annotated-types",
    "typing-extensions",
    "typing-inspection",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-out", type=Path, required=True)
    args = parser.parse_args()
    if args.image_out.exists() or args.image_out.is_symlink():
        raise ValueError("new owned image output required")
    args.image_out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="analysis-build-", dir=args.image_out.parent
    ) as folder:
        context = Path(folder)
        requirements = []
        for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]:
            if package["name"] in PACKAGES:
                requirements.append(
                    package["name"]
                    + "=="
                    + package["version"]
                    + " "
                    + " ".join(
                        "--hash=" + h
                        for h in sorted({w["hash"] for w in package["wheels"]})
                    )
                )
        if len(requirements) != len(PACKAGES):
            raise ValueError("incomplete dependency closure")
        (context / "requirements.txt").write_text(
            "\n".join(sorted(requirements)) + "\n"
        )
        for directory in (
            "analyzers",
            "admission",
            "contracts",
            "domain",
            "core/dsa",
            "security",
        ):
            shutil.copytree(
                ROOT / "backend/app" / directory,
                context / "backend/app" / directory,
                ignore=shutil.ignore_patterns("__pycache__", "* 2.py"),
            )
        shutil.copytree(
            ROOT / "backend/app/adapters",
            context / "backend/app/adapters",
            ignore=shutil.ignore_patterns("__pycache__", "github", "storage", "queue"),
        )
        # Runtime code imports no database stack; avoid shipping broker-side runner dependencies.
        (context / "backend/app/analyzers/runner.py").unlink()
        (context / "backend/app/admission/process.py").unlink()
        shutil.copytree(
            ROOT / "tools/typescript-analyzer",
            context / "tools/typescript-analyzer",
            ignore=shutil.ignore_patterns("node_modules", "dist"),
        )
        shutil.copyfile(ROOT / "infra/analysis/Dockerfile", context / "Dockerfile")
        digest = hashlib.sha256()
        for path in sorted(context.rglob("*")):
            if path.is_file():
                digest.update(path.relative_to(context).as_posix().encode())
                digest.update(path.read_bytes())
        toolchain = digest.hexdigest()
        (context / "toolchain.sha256").write_text(toolchain + "\n")
        subprocess.run(
            [
                "docker",
                "build",
                "--iidfile",
                str(args.image_out.absolute()),
                "--label",
                "codespy.phase3.toolchain-sha256=" + toolchain,
                str(context),
            ],
            check=True,
        )
    args.image_out.chmod(0o600)
    print("Built owned structural image; toolchain=" + toolchain)


if __name__ == "__main__":
    main()
