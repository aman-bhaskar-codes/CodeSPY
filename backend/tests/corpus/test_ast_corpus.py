"""Pinned real-repository corpus: floors on parse quality and fact density.

The live test downloads the repositories pinned in evals/ast/corpus.json from GitHub and is
skipped unless CODESPY_LIVE_CORPUS=1. The offline tests exercise the same runner on a
generated archive so the harness is covered in ordinary CI.
"""

import asyncio
import io
import json
import os
import zipfile

import pytest
from evals.ast import fetch, runner


def synthetic_archive(path):
    files = {
        "pkg/app.py": "import os\n\n\ndef run(a: int) -> int:\n    return a\n",
        "pkg/svc.go": "package svc\n\nfunc New(host string) *Server { return nil }\n",
        "pkg/Broken.java": "public class Broken { void x( { }\n",
        "pkg/README.md": "# not source\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr("owner-repo-abc123/" + name, text)
    path.write_bytes(buffer.getvalue())


def test_runner_measures_a_generated_archive_with_the_github_root_stripped(tmp_path):
    archive = tmp_path / "x.zip"
    synthetic_archive(archive)
    result = runner.measure("owner/repo", "a" * 40, archive)
    assert result["languages"] == {"python": 1, "go": 1, "java": 1}
    assert result["statuses"] == {"parsed": 2, "partial": 1}
    assert result["facts"]["symbols"] >= 3 and result["failures"] == []
    assert result["parse_success"] == 1 and result["clean_parse_ratio"] == pytest.approx(2 / 3)
    assert result["production_coverage"] == 1.0


def test_floors_report_every_shortfall_and_a_pin_mismatch():
    result = {
        "name": "o/r", "commit": "b" * 40, "statuses": {"parsed": 1}, "parse_success": 1,
        "clean_parse_ratio": 1, "facts": {"symbols": 1}, "failures": [{"path": "x"}],
    }  # fmt: skip
    manifest = {
        "repos": [{"name": "o/r", "commit": "a" * 40, "floors": {"min_files": 2, "min_symbols": 5}}]
    }
    problems = runner.check({"repos": [result]}, manifest)
    assert (
        len(problems) == 4
        and "pinned commit" in problems[0]
        and "extraction crashes" in problems[3]
    )


def test_every_corpus_entry_is_pinned_to_a_full_commit_and_has_floors():
    manifest = fetch.load()
    assert len(manifest["repos"]) >= 10
    for repo in manifest["repos"]:
        assert len(repo["commit"]) == 40 and set(repo["commit"]) <= set("0123456789abcdef")
        assert repo["floors"]["min_parse_success"] >= 0.99


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CODESPY_LIVE_CORPUS") != "1", reason="live GitHub access")
def test_pinned_corpus_meets_its_floors_and_is_deterministic():
    report = runner.run_corpus(asyncio.run(fetch.fetch()))
    assert runner.check(report, fetch.load()) == []
    again = runner.measure(
        "pallets/flask",
        report["repos"][0]["commit"],
        fetch.archive_path("pallets/flask", report["repos"][0]["commit"]),
    )
    for key in ("statuses", "facts", "error_nodes", "lines"):
        assert again[key] == report["repos"][0][key], key
    out = os.environ.get("CODESPY_CORPUS_REPORT")
    if out:
        with open(out, "w") as handle:
            json.dump(report, handle, indent=2)
