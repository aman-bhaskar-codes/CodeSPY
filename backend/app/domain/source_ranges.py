from typing import Self

from pydantic import Field, model_validator

from app.domain.scope import Contract


class SourceRangeV1(Contract):
    """Half-open UTF-8 bytes; one-based lines, zero-based byte columns."""

    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    start_col: int = Field(ge=0)
    end_col: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if (
            self.end_byte < self.start_byte
            or self.end_line < self.start_line
            or (self.start_line == self.end_line and self.end_col < self.start_col)
        ):
            raise ValueError("range end precedes start")
        return self
