"""Build metadata authenticates trusted local scanner artifacts."""

import json
import os
import subprocess
import sys
from pathlib import Path

from app.scanners.runtime import load_manifest


def test_build_manifest_binds_configured_artifact_and_rule_bytes(tmp_path):
    root = Path(__file__).resolve().parents[4]
    artifact = tmp_path / "engine"
    artifact.write_bytes(b"hello")
    rules = tmp_path / "rules"
    rules.write_bytes(b"hello")
    layout = tmp_path / "layout.json"
    layout.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "tool": "semgrep",
                        "version": "1.179.0",
                        "executable": str(artifact),
                        "artifact": str(artifact),
                        "rules": str(rules),
                        "rule_ids": ["known.rule"],
                    }
                ]
            }
        )
    )
    output = tmp_path / "runtime.json"
    result = subprocess.run(  # noqa: S603 - owned build script and synthetic fixture paths
        [sys.executable, str(root / "infra/scanner/build_manifest.py"), str(layout), str(output)],
        capture_output=True,
        timeout=10,
        env={"PATH": os.defpath, "PYTHONPATH": str(root / "backend")},
    )
    assert result.returncode == 0
    runtime = load_manifest(output).tools[0]
    assert (
        runtime.artifact_sha256
        == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
    assert runtime.rules_sha256 == runtime.artifact_sha256
