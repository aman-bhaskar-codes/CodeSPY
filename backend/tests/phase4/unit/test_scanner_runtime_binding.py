"""Runtime manifests authenticate only maintainer-owned regular artifacts."""

import hashlib

import pytest
from app.scanners.normalization import ScannerOutputError
from app.scanners.runtime import RuntimeManifest, ToolRuntime, load_manifest


def setup_manifest(root):
    executable = root / "scanner"
    rules = root / "rules"
    executable.write_bytes(b"trusted synthetic executable")
    rules.write_bytes(b"trusted synthetic rules")
    tool = ToolRuntime(
        tool="semgrep",
        version="1.179.0",
        executable=executable,
        artifact=executable,
        rules=rules,
        rule_ids=("known.rule",),
        artifact_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        rules_sha256=hashlib.sha256(rules.read_bytes()).hexdigest(),
    )
    manifest = RuntimeManifest(tools=(tool,))
    path = root / "runtime.json"
    path.write_text(manifest.model_dump_json())
    return path, manifest


def test_qualified_manifest_preserves_injected_profile_and_paths(tmp_path):
    path, expected = setup_manifest(tmp_path)
    actual = load_manifest(path)
    assert actual.select(expected.tools[0].profile()) == expected.tools[0]


@pytest.mark.parametrize(
    "change", ["artifact", "rules", "symlink", "manifest-symlink", "oversized", "invalid"]
)
def test_modified_runtime_artifact_or_manifest_cannot_be_qualified(tmp_path, change):
    path, _ = setup_manifest(tmp_path)
    if change in {"artifact", "rules"}:
        (tmp_path / ("scanner" if change == "artifact" else "rules")).write_bytes(b"changed")
    elif change == "symlink":
        target = tmp_path / "scanner"
        target.unlink()
        target.symlink_to(tmp_path / "rules")
    elif change == "manifest-symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif change == "oversized":
        path.write_bytes(b" " * 65537)
    else:
        path.write_bytes(b"{")
    with pytest.raises(ScannerOutputError) as caught:
        load_manifest(path)
    assert str(caught.value) == "scanner_output_invalid"
