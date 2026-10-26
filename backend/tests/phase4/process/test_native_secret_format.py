"""Synthetic format qualification on a trusted installed binary, separate from isolation."""

import hashlib
import json
import os
import shutil
from functools import cache
from pathlib import Path
from uuid import UUID

import pytest
from app.contracts.scanners import ScannerRequestV1
from app.scanners.commands import gitleaks_command
from app.scanners.normalization import normalize_gitleaks
from app.scanners.process import run_private

from tests.phase4.runtime_fixtures import runtime

FILE = UUID(int=1)
ALIAS = FILE.hex + ".txt"
RULE = Path(__file__).resolve().parents[4] / "infra/scanner/rules/secrets.toml"


@cache
def artifact_digest(binary):
    return hashlib.sha256(Path(binary).resolve().read_bytes()).hexdigest()


def cases():
    token = b"ghp_" + b"A" * 36
    positives = [
        (f"prefix-{n}", prefix + token + b"\n", len(prefix))
        for n, prefix in enumerate(
            (
                b"",
                b" ",
                b"token=",
                b'"',
                b"# ",
                "雪 ".encode(),
                b"first\n",
                b"first\r\n",
                b"a\nb\n",
                b"a\nb\r\n ",
            )
        )
    ]
    positives += [(f"no-final-newline-{n}", b" " * n + token, n) for n in range(10)]
    negatives = [(f"short-{n}", b"ghp_" + b"A" * n, None) for n in range(30, 36)]
    negatives += [(f"long-{n}", b"ghp_" + b"A" * n, None) for n in range(37, 43)]
    negatives += [
        (f"prefix-{n}", p + b"A" * 36, None)
        for n, p in enumerate((b"gho_", b"ghu_", b"ghs_", b"GHP_", b"ghp-", b"ghp "))
    ]
    negatives += [("empty", b"", None), ("ordinary", b"print('hello')\n", None)]
    return positives + negatives


@pytest.mark.parametrize("name,data,start", cases(), ids=[case[0] for case in cases()])
async def test_selected_native_format_has_attested_locations_and_safe_output(
    tmp_path, name, data, start
):
    binary = shutil.which("gitleaks")
    if binary is None:
        pytest.skip("trusted installed Gitleaks not available; not an isolation qualification")
    # Tool execution is opted in; arbitrary developer binaries are not auto-invoked.
    if os.environ.get("CODESPY_NATIVE_SCANNER_TESTS") != "1":
        pytest.skip("native scanner qualification not requested")
    version = await run_private((binary, "version"), deadline=5, stdout_limit=100, stderr_limit=100)
    assert version.exit_code == 0 and version.output.strip() == b"8.30.1"
    (tmp_path / ALIAS).write_bytes(data)
    command = list(gitleaks_command(ALIAS, runtime=runtime("gitleaks")))
    command[0] = binary
    command[command.index("--config") + 1] = str(RULE)
    ignore = tmp_path / "empty.ignore"
    ignore.write_bytes(b"")
    command[command.index("--gitleaks-ignore-path") + 1] = str(ignore)
    native = await run_private(
        tuple(command), cwd=tmp_path, deadline=5, stdout_limit=2097152, stderr_limit=65536
    )
    request = ScannerRequestV1.model_validate(
        {
            "scope": {"org_id": UUID(int=2), "snapshot_id": UUID(int=3), "run_id": UUID(int=4)},
            "repository_id": UUID(int=5),
            "config_hash": "a" * 64,
            "profile": {
                "tool": "gitleaks",
                "artifact_sha256": artifact_digest(binary),
                "rules_sha256": hashlib.sha256(RULE.read_bytes()).hexdigest(),
                "rule_ids": ("secret.github-classic-pat",),
            },
            "files": (
                {
                    "file_id": FILE,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                },
            ),
        }
    )
    result = normalize_gitleaks(
        request, {FILE: data}, {ALIAS: FILE}, native.output, exit_code=native.exit_code
    )
    assert result.status == "completed"
    assert len(result.observations) == int(start is not None), name
    if start is not None:
        span = result.observations[0].range
        assert (span.start_byte, span.end_byte) == (start, start + 40), name
        assert (b"ghp_" + b"A" * 36) not in result.model_dump_json().encode()
    assert "Secret" not in json.loads(result.model_dump_json())
