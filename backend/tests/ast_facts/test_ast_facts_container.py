"""Real Linux sandbox round trip; skipped without an owned analysis image."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from app.analyzers.facts_pipeline import analyze_facts
from app.analyzers.runner import AnalysisProcessError, DockerAnalysisRunner
from app.contracts.structure import AnalysisConfigV1
from app.domain.scope import RunRef
from app.services.ast_results import materialize_facts

from tests.phase3.unit.test_ast_facts_pipeline import source_input


async def test_container_facts_match_in_process_and_survive_broker_validation(tmp_path):
    image = os.environ.get("CODESPY_ANALYSIS_IMAGE")
    if not image:
        pytest.skip("owned Linux analyzer image required")
    runner = DockerAnalysisRunner(image, tmp_path / "owner")
    toolchain = await runner.qualify_analysis()
    config = AnalysisConfigV1(toolchain_hash=toolchain, adapters=("ast_facts",))
    root = tmp_path / "input"
    root.mkdir()
    source = source_input(root, config=config)
    expected = analyze_facts(source, root)
    draft = await runner.analyze_facts(source, root)
    assert draft == expected  # the sandbox yields exactly what in-process extraction yields
    result = materialize_facts(
        RunRef(org_id=uuid4(), snapshot_id=uuid4(), run_id=uuid4()), draft, source, root
    )
    assert result.batch.disposition == "partial" and len(result.digests) == 2


async def test_structural_requests_cannot_use_the_facts_entrypoint(tmp_path):
    runner = DockerAnalysisRunner("sha256:" + "a" * 64, tmp_path / "owner")
    root = tmp_path / "input"
    root.mkdir()
    source = source_input(root, config=AnalysisConfigV1(toolchain_hash="a" * 64))
    with pytest.raises(AnalysisProcessError, match="analysis_unavailable"):
        await runner.analyze_facts(source, root)


async def test_image_loads_and_parses_every_registered_grammar(tmp_path):
    image = os.environ.get("CODESPY_ANALYSIS_IMAGE")
    if not image:
        pytest.skip("owned Linux analyzer image required")
    from app.analyzers.registry import SPECS

    from tests.phase3.unit.test_language_registry import SAMPLES

    script = (
        "import json,sys\n"
        "from app.analyzers.registry import SPECS, parse_bytes\n"
        "samples=json.loads(sys.stdin.read())\n"
        "out={}\n"
        "for s in SPECS:\n"
        "    t=parse_bytes(s.name, samples[s.name].encode())\n"
        "    out[s.name]=not t.root_node.has_error\n"
        "print(json.dumps(out))\n"
    )
    command = [
        "docker", "run", "--rm", "-i", "--network", "none", "--read-only",
        "--user", "65534:65534", "--cap-drop", "ALL", "--entrypoint", "python",
        image, "-c", script,
    ]  # fmt: skip
    pipe = asyncio.subprocess.PIPE
    process = await asyncio.create_subprocess_exec(*command, stdin=pipe, stdout=pipe, stderr=pipe)
    payload = json.dumps({k: v.decode() for k, v in SAMPLES.items()}).encode()
    stdout, stderr = await asyncio.wait_for(process.communicate(payload), 60)
    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout) == {spec.name: True for spec in SPECS}
