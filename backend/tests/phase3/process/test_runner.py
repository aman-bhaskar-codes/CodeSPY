"""Linux isolation is required in CI; local runs explicitly skip without an owned image."""

import asyncio
import os

import pytest
from app.analyzers.runner import AnalysisProcessError, DockerAnalysisRunner, drain

from tests.phase3.unit.test_materialization import fixture_input


async def test_stream_flood_is_rejected_during_read():
    stream = asyncio.StreamReader()
    stream.feed_data(b"x" * 100)
    stream.feed_eof()
    with pytest.raises(AnalysisProcessError):
        await drain(stream, 99)


async def test_owned_linux_analyzer_round_trip(tmp_path):
    image = os.environ.get("CODESPY_ANALYSIS_IMAGE")
    if not image:
        pytest.skip("owned Linux analyzer image required")
    runner = DockerAnalysisRunner(image, tmp_path / "owner")
    source = fixture_input(tmp_path)
    toolchain = await runner.qualify_analysis()
    config = source.config.model_copy(update={"toolchain_hash": toolchain})
    source = source.model_copy(
        update={
            "config": config,
            "config_hash": config.semantic_hash(),
            "toolchain_hash": toolchain,
        }
    )
    result = await runner.analyze(source, tmp_path)
    assert result.symbols and result.toolchain_hash == toolchain
    assert result.request_nonce == source.request_nonce


async def test_wrong_image_is_rejected_before_execution(tmp_path):
    runner = DockerAnalysisRunner("sha256:" + "a" * 64, tmp_path / "owner")

    async def inspect(*args, **kwargs):
        return b'{"Labels":{},"Entrypoint":[]}'

    runner._command = inspect
    with pytest.raises(AnalysisProcessError, match="analysis_unavailable"):
        await runner.readiness()
