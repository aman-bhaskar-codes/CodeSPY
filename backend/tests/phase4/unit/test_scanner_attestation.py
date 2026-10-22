"""Independent original-byte range validation never invokes native scanners."""

import pytest
from app.contracts.scanners import ScannerObservationV1, ScannerResultV1
from app.domain.source_ranges import SourceRangeV1
from app.scanners.attestation import ScannerAttestationV1, attest
from tests.phase4.unit.test_scanner_archive import fixture_input


@pytest.mark.parametrize("bad", [False, True])
def test_result_coordinates_are_rechecked_against_original_archive(tmp_path, bad):
    source = fixture_input(tmp_path)
    file = source.request.files[0]
    span = SourceRangeV1(
        start_byte=0,
        end_byte=3,
        start_line=99 if bad else 1,
        end_line=99 if bad else 1,
        start_col=0,
        end_col=3,
    )
    result = ScannerResultV1(
        request_digest=source.request.semantic_hash(),
        status="completed",
        processed_file_ids=(file.file_id,),
        omitted_file_ids=(),
        observations=(
            ScannerObservationV1(
                rule_id="python.shell-true", file_id=file.file_id, sha256=file.sha256, range=span
            ),
        ),
    )
    result.validate_for(source.request)  # Metadata validation alone admits the bad coordinates.
    value = ScannerAttestationV1(source=source, result=result)
    if bad:
        with pytest.raises(ValueError):
            attest(value, tmp_path)
    else:
        assert len(attest(value, tmp_path)) == 64
