"""Worker request IO rejects foreign or corrupt bytes before native execution."""

import hashlib
from uuid import UUID

import pytest
from app.contracts.scanners import ScannerRequestV1
from app.scanners.entrypoint import read_sources
from app.scanners.normalization import ScannerOutputError

FILE = UUID(int=1)


def request():
    return ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
            "repository_id": UUID(int=5),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "semgrep",
                "artifact_sha256": "b" * 64,
                "rules_sha256": "c" * 64,
                "rule_ids": ("python.shell-true",),
            },
            "files": (
                {"file_id": FILE, "sha256": hashlib.sha256(b"pass\n").hexdigest(), "size_bytes": 5},
            ),
        }
    )


def test_worker_reads_only_generated_aliases_with_verified_bytes(tmp_path):
    (tmp_path / (FILE.hex + ".py")).write_bytes(b"pass\n")
    sources, aliases = read_sources(request(), tmp_path)
    assert sources == {FILE: b"pass\n"}
    assert aliases == {FILE.hex + ".py": FILE}


@pytest.mark.parametrize("change", ["symlink", "content", "missing", "directory"])
def test_worker_rejects_input_alias_substitution(tmp_path, change):
    target = tmp_path / (FILE.hex + ".py")
    if change == "symlink":
        other = tmp_path / "other"
        other.write_bytes(b"pass\n")
        target.symlink_to(other)
    elif change == "content":
        target.write_bytes(b"other")
    elif change == "directory":
        target.mkdir()
    with pytest.raises(ScannerOutputError):
        read_sources(request(), tmp_path)


def test_worker_native_timeout_has_a_distinct_safe_exit(tmp_path, monkeypatch):
    from app.scanners import entrypoint
    from app.scanners.process import NativeProcessError
    from app.scanners.runtime import RuntimeManifest

    from tests.phase4.runtime_fixtures import runtime
    from tests.phase4.unit.test_scanner_archive import fixture_input

    source = fixture_input(tmp_path)
    monkeypatch.setattr(entrypoint.sys, "argv", ["scanner", "--runtime", "/trusted/runtime.json"])
    monkeypatch.setattr(entrypoint.resource, "setrlimit", lambda *_: None)
    monkeypatch.setattr(
        entrypoint, "load_manifest", lambda _: RuntimeManifest(tools=(runtime("semgrep"),))
    )
    monkeypatch.setattr(entrypoint, "_read", lambda *_: source.model_dump_json().encode())

    async def timeout(*args, **kwargs):
        raise NativeProcessError("scanner_timeout")

    monkeypatch.setattr(entrypoint, "scan", timeout)
    assert entrypoint.main() == 75
