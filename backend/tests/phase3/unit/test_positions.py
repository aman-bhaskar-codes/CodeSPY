import pytest
from app.analyzers.positions import SourceIndex


def test_original_bytes_crlf_bom_and_astral_positions():
    raw = "\ufeffa😀\r\nbé\n".encode()
    index = SourceIndex(raw)
    span = index.range_from_bytes(4, 8)
    assert (span.start_line, span.start_col, span.end_line, span.end_col) == (1, 4, 1, 8)
    assert index.utf16_to_byte(2) == 4
    assert index.utf16_to_byte(4) == 8
    assert index.byte_to_utf16(8) == 4
    assert index.range_from_bytes(len(raw), len(raw)).start_col == 0
    assert index.range_from_bytes(10, 13).start_line == 2
    with pytest.raises(ValueError):
        index.utf16_to_byte(3)
    with pytest.raises(ValueError):
        index.range_from_bytes(5, 8)


def test_invalid_encoding_and_out_of_range_do_not_become_empty_source():
    with pytest.raises(ValueError):
        SourceIndex(b"\xff")
    index = SourceIndex(b"")
    assert index.range_from_bytes(0, 0).start_line == 1
    for start, end in [(-1, 0), (1, 1), (1, 0)]:
        with pytest.raises(ValueError):
            index.range_from_bytes(start, end)
