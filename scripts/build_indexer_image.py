"""Build an offline index image from explicit source and frozen binary dependencies."""

import argparse
import hashlib
import json
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOCABULARY_SHA256 = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
FILES = (
    "indexing/entrypoint.py",
    "indexing/materialize.py",
    "indexing/mapping.py",
    "indexing/budget.py",
    "ports/index_builder.py",
    "ports/source_reader.py",
    "source_read/entrypoint.py",
    "source_read/extract.py",
    "admission/zip_inventory.py",
    "services/privacy_transform.py",
    "contracts/indexing.py",
    "contracts/ingestion.py",
    "contracts/scanners.py",
    "contracts/source_view.py",
    "contracts/structure.py",
    "contracts/persistence.py",
    "contracts/evidence.py",
    "domain/canonical_json.py",
    "domain/errors.py",
    "domain/scope.py",
    "domain/source_ranges.py",
    "security/archive_paths.py",
)
PACKAGES = frozenset(
    {
        "pydantic",
        "pydantic-core",
        "annotated-types",
        "typing-extensions",
        "typing-inspection",
        "tiktoken",
        "regex",
        "requests",
        "charset-normalizer",
        "idna",
        "urllib3",
        "certifi",
    }
)


def prepare_context(
    target: Path, vocabulary: Path, *, wheelhouse: Path | None = None
) -> str:
    if (
        vocabulary.is_symlink()
        or not vocabulary.is_file()
        or vocabulary.stat().st_size > 2_097_152
    ):
        raise ValueError("verified vocabulary required")
    raw = vocabulary.read_bytes()
    if hashlib.sha256(raw).hexdigest() != VOCABULARY_SHA256:
        raise ValueError("verified vocabulary required")
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    selected = {p["name"]: p for p in lock["package"] if p["name"] in PACKAGES}
    if selected.keys() != PACKAGES:
        raise ValueError("incomplete frozen dependency closure")
    lines = []
    for name, package in sorted(selected.items()):
        if any(dep["name"] not in PACKAGES for dep in package.get("dependencies", [])):
            raise ValueError("incomplete frozen dependency closure")
        hashes = sorted({w["hash"] for w in package.get("wheels", [])})
        if not hashes:
            raise ValueError("hash-locked binary dependencies required")
        lines.append(
            name
            + "=="
            + package["version"]
            + " "
            + " ".join("--hash=" + h for h in hashes)
        )
    requirements = ("\n".join(lines) + "\n").encode()
    dockerfile = (ROOT / "infra/indexer/Dockerfile").read_bytes()
    wheel_digests = []
    if wheelhouse is not None:
        allowed = {
            w["url"].rsplit("/", 1)[-1]: (name, w["hash"])
            for name, package in selected.items()
            for w in package.get("wheels", [])
        }
        seen = set()
        destination = target / "wheels"
        destination.mkdir()
        for wheel in sorted(wheelhouse.iterdir()):
            if (
                wheel.is_symlink()
                or not wheel.is_file()
                or wheel.stat().st_size > 33_554_432
            ):
                raise ValueError("invalid offline wheel")
            identity = allowed.get(wheel.name)
            if identity is None:
                raise ValueError("unlocked offline wheel")
            name, expected = identity
            content = wheel.read_bytes()
            actual = "sha256:" + hashlib.sha256(content).hexdigest()
            if actual != expected or name in seen:
                raise ValueError("invalid offline wheel")
            seen.add(name)
            (destination / wheel.name).write_bytes(content)
            wheel_digests.append(wheel.name + actual)
        if seen != PACKAGES:
            raise ValueError("incomplete offline wheel closure")
        dockerfile = dockerfile.replace(
            b"RUN PIP_CONFIG_FILE", b"COPY wheels /wheels\nRUN PIP_CONFIG_FILE"
        )
        dockerfile = dockerfile.replace(
            b"--index-url https://pypi.org/simple", b"--no-index --find-links=/wheels"
        )
    (target / "requirements.txt").write_bytes(requirements)
    (target / "Dockerfile").write_bytes(dockerfile)
    (target / "vocabulary").write_bytes(raw)
    digest = hashlib.sha256(
        dockerfile + requirements + raw + "".join(wheel_digests).encode()
    )
    for file in sorted(FILES):
        source = ROOT / "backend/app" / file
        destination = target / "app" / file
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        destination.write_bytes(content)
        digest.update(file.encode())
        digest.update(content)
    source_hash = digest.hexdigest()
    (target / "runtime.json").write_text(
        json.dumps({"runtime_source_sha256": source_hash})
    )
    return source_hash


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-out", type=Path, required=True)
    parser.add_argument("--vocabulary", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args()
    if args.image_out.exists() or args.image_out.is_symlink():
        raise ValueError("new owned image output required")
    args.image_out.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codespy-index-build-") as directory:
        target = Path(directory)
        source_hash = prepare_context(
            target, args.vocabulary, wheelhouse=args.wheelhouse
        )
        subprocess.run(
            [
                "docker",
                "build",
                "--force-rm",
                "--label",
                "codespy.indexer.source-hash=" + source_hash,
                "--iidfile",
                str(args.image_out.absolute()),
                str(target),
            ],
            check=True,
        )
    args.image_out.chmod(0o600)
    print(
        json.dumps(
            {"image_id": args.image_out.read_text().strip(), "source_hash": source_hash}
        )
    )


if __name__ == "__main__":
    main()
