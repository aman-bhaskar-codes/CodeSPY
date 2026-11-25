"""The owner-run smoke script: it refuses without opt-in, and its offline half works alone."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "narrative_smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("narrative_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_script_refuses_without_the_opt_in_and_without_a_profile(smoke, monkeypatch, capsys):
    monkeypatch.delenv("CODESPY_LIVE_BEDROCK", raising=False)
    monkeypatch.setattr(sys, "argv", ["x", "--converse-model", "m", "--demo"])
    assert smoke.main() == 2 and "CODESPY_LIVE_BEDROCK=1" in capsys.readouterr().out
    monkeypatch.setenv("CODESPY_LIVE_BEDROCK", "1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    assert smoke.main() == 2 and "AWS_PROFILE" in capsys.readouterr().out
    monkeypatch.setenv("AWS_PROFILE", "p")
    monkeypatch.setattr(sys, "argv", ["x", "--converse-model", "m", "--demo", "--max-pages", "99"])
    assert smoke.main() == 2
    monkeypatch.setattr(sys, "argv", ["x", "--converse-model", "m"])
    assert smoke.main() == 2  # neither --demo nor --report-file


def test_the_demo_report_builds_pages_the_verifier_can_use_offline(smoke):
    from app.analyzers.graph.report_pages import build_page
    from app.contracts.report_narrative import NarrativeConclusionV1, NarrativeSentenceV1
    from app.contracts.source_view import PrivacyPolicyV1
    from app.services.narrative_verification import verify_narrative

    report = smoke.demo_report()
    page = build_page(report, "overview", 0, PrivacyPolicyV1())
    assert page is not None and page.entries
    shown = {e["sid"]: e for e in page.entries}
    first = page.entries[0]
    sentence = NarrativeSentenceV1(claim_id="c1", text=first["text"], sids=(first["sid"],))
    outcome = verify_narrative(NarrativeConclusionV1(sentences=(sentence,)), shown)
    assert outcome.confirmed == 1
