"""Real offline admitted metadata assessment in owned Linux containers."""

from datetime import timedelta
from pathlib import Path

import pytest
from app.contracts.dependencies import DependencyRequestV1
from app.contracts.ingestion import AdmissionPolicyV1
from app.scanners.advisories import AdvisoryBundle, ExactAnswer, PackageQuery
from app.scanners.dependency_adapter import DockerDependencyAdapter
from app.scanners.docker import DockerController
from app.scanners.ownership import ScannerOwnership
from app.services.findings import FindingMaterializer, ScanPublicationSpecV1, reviewed_catalog

from tests.phase4.dependency_fixtures import NOW
from tests.phase4.process.test_scanner_image import settings
from tests.phase4.unit.test_advisory_bundle import answer, record
from tests.phase4.unit.test_dependency_adapter import prepare


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "missing",
        "matched",
        "stale",
        "withdrawn",
        "incomplete",
        "excluded",
        "invalid",
        "uv-empty",
        "uv-unresolved",
    ],
)
async def test_real_admitted_lock_evidence_never_turns_unknown_into_empty(tmp_path, case):
    binary, socket, image = settings()
    options = {}
    if case == "missing":
        options["bundle"] = AdvisoryBundle(answers=(), records=())
    elif case in {"matched", "stale", "withdrawn", "incomplete"}:
        advisory = record()
        document = dict(advisory.document)
        document["summary"] = "PRIVATE-CANARY"
        if case == "withdrawn":
            document["withdrawn"] = NOW.isoformat()
        advisory = advisory.model_copy(update={"document": document})
        response = answer(advisory_ids=(advisory.id,))
        if case == "stale":
            response = response.model_copy(update={"fetched_at": NOW - timedelta(days=8)})
        elif case == "incomplete":
            response = response.model_copy(update={"complete": False})
        options["bundle"] = AdvisoryBundle(answers=(response,), records=(advisory,))
    elif case == "excluded":
        options.update(policy=AdmissionPolicyV1(), contents={"uv.lock": b"PRIVATE-CANARY"})
    elif case == "invalid":
        options["contents"] = {"package-lock.json": b"PRIVATE-CANARY"}
    elif case.startswith("uv-"):
        registry = (
            "https://pypi.org/simple" if case == "uv-empty" else "https://private.invalid/simple"
        )
        contents = "version=1\nrevision=3\n"
        for platform in ("linux", "darwin"):
            contents += f'''[[package]]
name="example"
version="1.2.3"
source={{registry="{registry}"}}
resolution-markers=["sys_platform == '{platform}'"]
'''
        options["contents"] = {"uv.lock": contents.encode()}
        options["bundle"] = AdvisoryBundle(
            answers=(
                ExactAnswer(
                    query=PackageQuery(ecosystem="PyPI", name="example", version="1.2.3"),
                    fetched_at=NOW,
                    complete=True,
                    advisory_ids=(),
                ),
            ),
            records=(),
        )
    request, bundle, stager = await prepare(tmp_path, **options)
    request = DependencyRequestV1.model_validate(
        {**request.model_dump(), "runtime_digest": image.removeprefix("sha256:")}
    )
    owner = ScannerOwnership.create(tmp_path / "owned", image)
    controller = DockerController(Path(binary), Path(socket), tmp_path / "docker-config")
    adapter = DockerDependencyAdapter(
        owner,
        controller,
        stager,
        bundle,
        expected_entrypoint=await controller.image_entrypoint(image),
    )
    receipt = await adapter.assess_attested(request)
    result = receipt.result
    result.validate_for(request)
    expected = {
        "empty": "complete",
        "matched": "complete",
        "missing": "partial",
        "stale": "partial",
        "withdrawn": "partial",
        "incomplete": "partial",
        "excluded": "unknown",
        "invalid": "unknown",
        "uv-empty": "complete",
        "uv-unresolved": "partial",
    }[case]
    assert result.coverage == expected
    if case in {"empty", "matched", "missing", "stale", "withdrawn", "incomplete"}:
        assert len(result.packages) == 1
        assert result.packages[0].assessment.status == case
    elif case == "uv-empty":
        assert len(result.packages) == 2
        assert {p.location for p in result.packages} == {"package[0]", "package[1]"}
        assert all(
            p.marker_count == 1 and p.environment_status == "unresolved" for p in result.packages
        )
    else:
        assert result.packages == ()
    if case == "uv-unresolved":
        assert {p.location for p in result.unresolved} == {"package[0]", "package[1]"}
    assert "PRIVATE-CANARY" not in result.model_dump_json()
    catalog = reviewed_catalog(())
    draft = FindingMaterializer(catalog).materialize(
        ScanPublicationSpecV1(
            scope=request.scope,
            repository_id=request.repository_id,
            config_hash=request.config_hash,
            runtime_digest=request.runtime_digest,
            catalog_digest=catalog.semantic_hash(),
            captured_at=NOW,
            dependency_request=request,
        ),
        (),
        dependency=receipt,
        bundle=bundle,
    )
    assert draft.coverage == expected
    assert len(draft.findings) == (1 if case == "matched" else 0)
    assert "PRIVATE-CANARY" not in draft.model_dump_json()
    assert list(owner.root.glob("codespy-scan-*")) == []
