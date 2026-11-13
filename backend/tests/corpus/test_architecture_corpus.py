# ruff: noqa: E501
"""Architecture report over the pinned corpus: floors, determinism and an offline harness check."""

import os

import pytest
from evals.ast import fetch, report_eval

from tests.corpus.test_ast_corpus import synthetic_archive


def comparable(result):
    return {k: v for k, v in result.items() if k not in {"seconds", "digest"}}


def test_the_report_harness_measures_a_generated_archive_deterministically(tmp_path):
    archive = tmp_path / "x.zip"
    synthetic_archive(archive)
    first = report_eval.measure_report("o/r", "a" * 40, archive)
    second = report_eval.measure_report("o/r", "a" * 40, archive)
    assert comparable(first) == comparable(second)
    assert first["files"] == 3 and first["sections"] == report_eval.SECTIONS
    assert first["analytics"] == "complete" and first["shards"] == 1


def test_floor_checks_report_every_violation():
    result = {
        "name": "o/r", "sections": 7, "analytics": "wall", "resolved": 1, "unresolved_permille": 500,
        "ambiguous_permille": 0, "components": 1, "report_kib": 900, "seconds": 9.0,
    }  # fmt: skip
    manifest = {
        "repos": [
            {
                "name": "o/r",
                "report_floors": {
                    "min_resolved": 5,
                    "max_unresolved_permille": 100,
                    "max_report_kib": 100,
                    "max_seconds": 5.0,
                },
            }
        ]
    }
    problems = report_eval.check_report([result], manifest)
    assert len(problems) == 6 and any("expected 8 sections" in p for p in problems)
    passing = {
        **result,
        "sections": 8,
        "analytics": "complete",
        "resolved": 9,
        "unresolved_permille": 1,
        "report_kib": 5,
        "seconds": 0.1,
    }
    assert report_eval.check_report([passing], manifest) == []


def test_every_pinned_repository_has_report_floors():
    for repo in fetch.load()["repos"]:
        assert {"min_resolved", "max_unresolved_permille", "max_report_kib"} <= set(
            repo["report_floors"]
        )


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("CODESPY_LIVE_CORPUS") != "1", reason="live GitHub access")
def test_pinned_corpus_reports_meet_their_floors():
    import asyncio

    fetched = asyncio.run(fetch.fetch())
    results = [report_eval.measure_report(n, c, p) for n, c, p, _ in fetched]
    assert report_eval.check_report(results, fetch.load()) == []
    again = report_eval.measure_report(*fetched[0][:3])
    assert comparable(again) == comparable(results[0])
