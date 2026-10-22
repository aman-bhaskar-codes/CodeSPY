"""Build-only capture of maintainer-selected runtime artifact identities."""

import hashlib
import json
import sys
from pathlib import Path

from app.scanners.runtime import RuntimeManifest, load_manifest


def main(layout_path: Path, output_path: Path) -> None:
    raw = layout_path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("runtime layout exceeds bound")
    document = json.loads(raw)
    for tool in document["tools"]:
        tool["artifact_sha256"] = "0" * 64
        tool["rules_sha256"] = "0" * 64
    manifest = RuntimeManifest.model_validate_json(json.dumps(document))
    tools = []
    for tool in manifest.tools:
        identities = {}
        for field, path in [
            ("artifact_sha256", tool.artifact),
            ("rules_sha256", tool.rules),
        ]:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 268435456
            ):
                raise ValueError("invalid runtime artifact")
            with path.open("rb") as stream:
                identities[field] = hashlib.file_digest(stream, "sha256").hexdigest()
        tools.append(tool.model_copy(update=identities))
    with output_path.open("xb") as output:
        output.write(RuntimeManifest(tools=tuple(tools)).model_dump_json().encode())
    load_manifest(output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(64)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
