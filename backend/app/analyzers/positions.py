"""Original UTF-8/UTF-16 boundaries; no normalization of source text."""

from array import array
from bisect import bisect_left, bisect_right

from app.domain.source_ranges import SourceRangeV1


class SourceIndex:
    def __init__(self, data: bytes):
        if len(data) > 2097152:
            raise ValueError("source byte limit")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ValueError("source encoding invalid") from None
        self.data = data
        self._lines = array("I", [0])
        self._bytes = array("I", [0])
        self._utf16 = array("I", [0])
        byte = units = 0
        for char in text:
            byte += len(char.encode("utf-8"))
            units += 2 if ord(char) > 0xFFFF else 1
            self._bytes.append(byte)
            self._utf16.append(units)
            if char == "\n":
                self._lines.append(byte)

    @staticmethod
    def _lookup(values: array[int], offset: int) -> int:
        if type(offset) is not int or offset < 0:
            raise ValueError("source offset invalid")
        index = bisect_left(values, offset)
        if index >= len(values) or values[index] != offset:
            raise ValueError("source offset is not a character boundary")
        return index

    def utf16_to_byte(self, offset: int) -> int:
        return self._bytes[self._lookup(self._utf16, offset)]

    def byte_to_utf16(self, offset: int) -> int:
        return self._utf16[self._lookup(self._bytes, offset)]

    def range_from_bytes(self, start: int, end: int) -> SourceRangeV1:
        self._lookup(self._bytes, start)
        self._lookup(self._bytes, end)
        if end < start:
            raise ValueError("source range reversed")
        first = bisect_right(self._lines, start) - 1
        last = bisect_right(self._lines, end) - 1
        return SourceRangeV1(
            start_byte=start,
            end_byte=end,
            start_line=first + 1,
            end_line=last + 1,
            start_col=start - self._lines[first],
            end_col=end - self._lines[last],
        )

    def validate_range(self, span: SourceRangeV1) -> None:
        if self.range_from_bytes(span.start_byte, span.end_byte) != span:
            raise ValueError("source location does not match original bytes")
