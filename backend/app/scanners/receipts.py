"""Internal broker execution receipts; no wire decoder or bearer authority.

Trusted adapters issue receipts after containment and source verification. This
separates a validated DTO from a completed broker operation; Python object
privacy is not a security boundary against arbitrary code in the broker process.
"""

from dataclasses import dataclass, field

from app.contracts.dependencies import DependencyRequestV1, DependencyResultV1
from app.contracts.scanners import ScannerRequestV1, ScannerResultV1
from app.domain.canonical_json import semantic_digest

_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class BrokerReceipt[
    Request: (ScannerRequestV1, DependencyRequestV1),
    Result: (ScannerResultV1, DependencyResultV1),
]:
    request: Request
    result: Result
    image: str
    provider_version: str
    result_digest: str
    _issuer: object = field(repr=False)

    def validate(self) -> None:
        if getattr(self, "_issuer", None) is not _ISSUER:
            raise ValueError("broker execution receipt required")
        request, result = self.request, self.result
        if isinstance(request, ScannerRequestV1) and isinstance(result, ScannerResultV1):
            ScannerRequestV1.model_validate(request.model_dump())
            ScannerResultV1.model_validate(result.model_dump()).validate_for(request)
        elif isinstance(request, DependencyRequestV1) and isinstance(result, DependencyResultV1):
            DependencyRequestV1.model_validate(request.model_dump())
            DependencyResultV1.model_validate(result.model_dump()).validate_for(request)
        else:
            raise ValueError("broker result contract differs")
        if self.result_digest != semantic_digest(result.model_dump()):
            raise ValueError("broker result digest differs")


def _issue[
    Request: (ScannerRequestV1, DependencyRequestV1),
    Result: (ScannerResultV1, DependencyResultV1),
](
    request: Request, result: Result, image: str, provider_version: str
) -> BrokerReceipt[Request, Result]:
    value: BrokerReceipt[Request, Result] = object.__new__(BrokerReceipt)
    for name, item in (
        ("request", request),
        ("result", result),
        ("image", image),
        ("provider_version", provider_version),
        ("result_digest", semantic_digest(result.model_dump())),
        ("_issuer", _ISSUER),
    ):
        object.__setattr__(value, name, item)
    value.validate()
    return value
