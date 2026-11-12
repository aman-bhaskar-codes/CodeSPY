"""Opt-in live Bedrock checks. Never run in CI: they need the owner's profile and cost money."""

import os

import pytest

pytestmark = pytest.mark.live_bedrock


def test_smoke_script_refuses_without_the_opt_in(monkeypatch, capsys):
    import sys

    monkeypatch.delenv("CODESPY_LIVE_BEDROCK", raising=False)
    monkeypatch.setattr(sys, "argv", ["bedrock_smoke.py", "--converse-model", "x"])
    from scripts import bedrock_smoke

    assert bedrock_smoke.main() == 2
    assert "refusing" in capsys.readouterr().out


@pytest.mark.skipif(os.environ.get("CODESPY_LIVE_BEDROCK") != "1", reason="paid live calls")
async def test_live_converse_and_embedding_round_trip():
    from scripts import bedrock_smoke

    model = os.environ["CODESPY_LIVE_BEDROCK_MODEL"]
    assert await bedrock_smoke.run(os.environ["AWS_PROFILE"], model, False) == 0
