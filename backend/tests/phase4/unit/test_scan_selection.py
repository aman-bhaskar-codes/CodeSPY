from uuid import uuid4

from app.contracts.scan_jobs import NativeSelectionV1, ScanConfigV1
from app.contracts.scanners import ScannerProfileV1
from app.contracts.structure import SourceFileV1
from app.services.scan_selection import choose_scan_configuration


def test_selection_preserves_eligibility_and_resource_limits():
    template = ScanConfigV1(
        runtime_digest="a" * 64,
        catalog_digest="b" * 64,
        native_selections=(
            NativeSelectionV1(
                profile=ScannerProfileV1(
                    tool="semgrep",
                    artifact_sha256="c" * 64,
                    rules_sha256="d" * 64,
                    rule_ids=("fixture.rule",),
                ),
                files=(),
            ),
        ),
    )
    files = tuple(
        SourceFileV1(
            file_id=uuid4(), path=f"{n:03}.py", sha256="e" * 64, size_bytes=131072, eligible=True
        )
        for n in range(130)
    )
    private = SourceFileV1(
        file_id=uuid4(),
        path="private.py",
        sha256="f" * 64,
        size_bytes=1,
        eligible=False,
        exclusion_reason="private",
    )
    result = choose_scan_configuration(template, (*files, private))
    assert result.config is not None
    selected = result.config.native_selections[0].files
    assert len(selected) == 128
    assert sum(file.size_bytes for file in selected) == 16777216
    assert private.file_id not in {file.file_id for file in selected}
    assert result.omitted_source_files == 2
    assert (
        choose_scan_configuration(template, tuple(reversed((*files, private)))).config
        == result.config
    )
