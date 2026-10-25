"""In-process preparation provenance, not bearer or database publication authority."""

from dataclasses import dataclass, field

from app.contracts.indexing import IndexPublicationDraftV1, IndexSpecV1

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class PreparedIndexReceipt:
    spec: IndexSpecV1 = field(repr=False)
    draft: IndexPublicationDraftV1 = field(repr=False)
    spec_hash: str
    draft_digest: str
    image: str
    runtime_source_sha256: str
    _issuer: object = field(repr=False)

    def validate(self) -> None:
        if getattr(self, "_issuer", None) is not _ISSUER:
            raise ValueError("index preparation receipt required")
        spec = IndexSpecV1.model_validate(self.spec.model_dump())
        draft = IndexPublicationDraftV1.model_validate(self.draft.model_dump())
        draft.validate_for(spec)
        if (
            self.spec_hash != spec.semantic_hash()
            or self.draft_digest != draft.semantic_hash()
            or self.image != spec.config.runtime_image
            or self.runtime_source_sha256 != spec.config.runtime_source_sha256
        ):
            raise ValueError("index preparation identity differs")


def _issue(spec: IndexSpecV1, draft: IndexPublicationDraftV1) -> PreparedIndexReceipt:
    receipt: PreparedIndexReceipt = object.__new__(PreparedIndexReceipt)
    for name, value in (
        ("spec", spec),
        ("draft", draft),
        ("spec_hash", spec.semantic_hash()),
        ("draft_digest", draft.semantic_hash()),
        ("image", spec.config.runtime_image),
        ("runtime_source_sha256", spec.config.runtime_source_sha256),
        ("_issuer", _ISSUER),
    ):
        object.__setattr__(receipt, name, value)
    receipt.validate()
    return receipt
