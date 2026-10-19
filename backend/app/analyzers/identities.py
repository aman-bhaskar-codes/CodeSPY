"""Content matching keys stay separate from scoped physical identities."""

from typing import Any

from app.contracts.structure import ArchitectureReportV1, SymbolObservationV1
from app.domain.canonical_json import semantic_digest


def observation_key(record: SymbolObservationV1) -> str:
    return semantic_digest(
        {
            "version": 1,
            "path": record.path,
            "file_sha256": record.sha256,
            "kind": record.kind,
            "range": record.location.range.model_dump(),
            "qualified_name": record.qualified_name,
            "role": record.role,
        }
    )


def normalize_report(report: ArchitectureReportV1) -> dict[str, Any]:
    data = report.model_dump(mode="json")
    data.pop("run_ref", None)
    data.pop("request_nonce", None)
    data.pop("symbol_map", None)
    data.pop("evidence_map", None)
    files = {str(s.file_id): {"path": s.path, "sha256": s.sha256} for s in report.symbols}

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if key == "file_id":
                    result["source"] = files.get(str(item), {"sha256": value.get("sha256")})
                else:
                    result[key] = clean(item)
            return result
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    result = clean(data)
    return result if isinstance(result, dict) else {}
