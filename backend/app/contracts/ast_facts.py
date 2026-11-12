"""Evidence-first per-file AST facts; every fact cites original source bytes and lines."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.contracts.structure import Hash, Language, StructuralCoverageV1
from app.domain.canonical_json import semantic_digest
from app.domain.scope import Contract
from app.domain.source_ranges import SourceRangeV1

AST_FACTS_SCHEMA = 1
MAX_FILE_FACTS = 5000
Name = str


class ParameterV1(Contract):
    name: str = Field(min_length=1, max_length=256)
    type: str | None = Field(default=None, max_length=512)
    default: str | None = Field(default=None, max_length=512)
    kind: Literal["positional", "keyword_only", "variadic", "variadic_keyword"] = "positional"
    optional: bool = False


class SignatureV1(Contract):
    params: tuple[ParameterV1, ...] = Field(default=(), max_length=128)
    return_type: str | None = Field(default=None, max_length=512)


class SymbolFactV1(Contract):
    kind: Literal[
        "function",
        "method",
        "class",
        "interface",
        "struct",
        "enum",
        "trait",
        "type_alias",
        "constant",
        "variable",
        "field",
        "namespace",
    ]
    name: Name = Field(min_length=1, max_length=512)
    qualified_name: str = Field(min_length=1, max_length=2048)
    range: SourceRangeV1
    visibility: Literal["public", "protected", "private", "internal"] | None = None
    modifiers: tuple[str, ...] = Field(default=(), max_length=16)
    decorators: tuple[str, ...] = Field(default=(), max_length=32)
    signature: SignatureV1 | None = None
    bases: tuple[str, ...] = Field(default=(), max_length=32)
    implements: tuple[str, ...] = Field(default=(), max_length=32)
    exported: bool = False
    container: str | None = Field(default=None, max_length=2048)


class ImportNameV1(Contract):
    name: str = Field(min_length=1, max_length=512)
    alias: str | None = Field(default=None, max_length=512)


class ImportFactV1(Contract):
    kind: Literal["static", "from", "wildcard", "dynamic", "require", "reexport"]
    module: str = Field(max_length=2048)
    relative_level: int = Field(default=0, ge=0, le=32)
    names: tuple[ImportNameV1, ...] = Field(default=(), max_length=256)
    alias: str | None = Field(default=None, max_length=512)
    type_only: bool = False
    range: SourceRangeV1


class ExportFactV1(Contract):
    name: str = Field(min_length=1, max_length=512)
    kind: Literal["declared", "all_list", "default", "reexport"]
    from_module: str | None = Field(default=None, max_length=2048)
    range: SourceRangeV1


class ColumnFactV1(Contract):
    name: str = Field(min_length=1, max_length=256)
    type: str | None = Field(default=None, max_length=256)
    nullable: bool | None = None
    primary_key: bool = False
    foreign_key: str | None = Field(default=None, max_length=512)


class ModelFactV1(Contract):
    framework: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=512)
    table: str | None = Field(default=None, max_length=256)
    columns: tuple[ColumnFactV1, ...] = Field(default=(), max_length=256)
    range: SourceRangeV1


class RouteFactV1(Contract):
    framework: str = Field(min_length=1, max_length=64)
    method: str = Field(min_length=1, max_length=16)
    path: str = Field(max_length=2048)
    handler: str | None = Field(default=None, max_length=2048)
    range: SourceRangeV1


class ConfigFactV1(Contract):
    kind: Literal["env_var", "config_key"]
    name: str = Field(min_length=1, max_length=512)
    range: SourceRangeV1


class DiagnosticFactV1(Contract):
    code: Literal["syntax_error", "fact_capacity", "unsupported_construct"]
    message: str = Field(max_length=256)
    range: SourceRangeV1 | None = None


class FileFactsV1(Contract):
    schema_version: Literal[1] = 1
    file_id: UUID
    path: str = Field(min_length=1, max_length=1024)
    sha256: Hash
    language: Language
    grammar: str = Field(min_length=1, max_length=128)
    status: Literal["parsed", "partial"]
    error_nodes: int = Field(default=0, ge=0)
    symbols: tuple[SymbolFactV1, ...] = Field(default=(), max_length=MAX_FILE_FACTS)
    imports: tuple[ImportFactV1, ...] = Field(default=(), max_length=MAX_FILE_FACTS)
    exports: tuple[ExportFactV1, ...] = Field(default=(), max_length=MAX_FILE_FACTS)
    routes: tuple[RouteFactV1, ...] = Field(default=(), max_length=256)
    models: tuple[ModelFactV1, ...] = Field(default=(), max_length=256)
    config: tuple[ConfigFactV1, ...] = Field(default=(), max_length=1024)
    diagnostics: tuple[DiagnosticFactV1, ...] = Field(default=(), max_length=64)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.status == "parsed") != (self.error_nodes == 0):
            raise ValueError("status and error node count disagree")
        return self

    def facts_sha256(self) -> str:
        return semantic_digest(self.model_dump(mode="json"))


class AstDraftV1(Contract):
    """Container output, validated again by the broker against the original archive."""

    schema_version: Literal[1] = 1
    request_nonce: str = Field(min_length=1, max_length=128)
    config_hash: Hash
    toolchain_hash: Hash
    archive_sha256: Hash
    policy_hash: Hash
    files: tuple[FileFactsV1, ...] = Field(default=(), max_length=1000)
    coverage: StructuralCoverageV1

    @model_validator(mode="after")
    def unique_files(self) -> Self:
        if len({f.file_id for f in self.files}) != len(self.files):
            raise ValueError("duplicate file facts")
        return self


class AstFileSummaryV1(Contract):
    file_id: UUID
    path: str
    language: Language
    status: Literal["parsed", "partial"]
    facts_sha256: Hash
    symbols: int = Field(ge=0)
    imports: int = Field(ge=0)
    exports: int = Field(ge=0)


class AstSnapshotSummaryV1(Contract):
    run_id: UUID
    snapshot_id: UUID
    files: int = Field(ge=0)
    languages: dict[str, int]
    symbols: int = Field(ge=0)
    imports: int = Field(ge=0)
    exports: int = Field(ge=0)


class AstFilePageV1(Contract):
    run_id: UUID
    items: tuple[AstFileSummaryV1, ...] = Field(max_length=200)
    next_after: str | None = None
