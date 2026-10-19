"""Fixed trusted TypeScript protocol. Positions are original UTF-16 code units."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


class TypeScriptProtocolError(ValueError):
    pass


def _path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 1024
        and "\\" not in value
        and "\x00" not in value
        and not value.startswith("__trusted/")
        and all(p not in ("", ".", "..") for p in value.split("/"))
    )


def validate_input(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or set(value) - {"schema_version", "files", "configs", "max_sites"}
    ):
        raise TypeScriptProtocolError("invalid input")
    files = value.get("files")
    configs = value.get("configs", [])
    if (
        not isinstance(files, list)
        or not isinstance(configs, list)
        or len(files) + len(configs) > 128
    ):
        raise TypeScriptProtocolError("invalid files")
    names, size = set(), 0
    for item in files + configs:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "text"}
            or not _path(item["path"])
            or not isinstance(item["text"], str)
            or item["path"] in names
        ):
            raise TypeScriptProtocolError("invalid file")
        names.add(item["path"])
        length = len(item["text"].encode("utf-8"))
        size += length
        if length > 2 * 1024 * 1024:
            raise TypeScriptProtocolError("file limit")
    maximum = value.get("max_sites", 2000)
    if type(maximum) is not int or not 1 <= maximum <= 2000 or size > 16 * 1024 * 1024:
        raise TypeScriptProtocolError("input limit")
    return value


def validate_result(value: Any, request: dict[str, Any]) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "resolutions", "diagnostics", "truncated"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["truncated"]) is not bool
    ):
        raise TypeScriptProtocolError("invalid result")
    lengths = {f["path"]: len(f["text"].encode("utf-16-le")) // 2 for f in request["files"]}

    def span(x: dict[str, Any]) -> bool:
        return (
            x.get("path") in lengths
            and type(x.get("start")) is int
            and type(x.get("end")) is int
            and 0 <= x["start"] <= x["end"] <= lengths[x["path"]]
        )

    rows = value["resolutions"]
    if not isinstance(rows, list) or len(rows) > request.get("max_sites", 2000):
        raise TypeScriptProtocolError("resolution limit")
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "path",
                "start",
                "end",
                "namespace",
                "resolution_kind",
                "status",
                "candidates",
                "reason",
                "basis",
            }
            or not span(row)
            or row["namespace"] not in ("value", "type")
            or row["resolution_kind"] not in ("binding", "call_target", "import")
            or row["status"] not in ("resolved", "ambiguous", "unresolved")
            or row["basis"] != "compiler_symbol"
            or not isinstance(row["reason"], str)
            or len(row["reason"]) > 1024
        ):
            raise TypeScriptProtocolError("invalid resolution")
        candidates = row["candidates"]
        if not isinstance(candidates, list) or len(candidates) > 2000:
            raise TypeScriptProtocolError("candidate limit")
        for candidate in candidates:
            if (
                not isinstance(candidate, dict)
                or set(candidate) != {"path", "start", "end", "name"}
                or not span(candidate)
                or not isinstance(candidate["name"], str)
                or len(candidate["name"]) > 4096
            ):
                raise TypeScriptProtocolError("invalid candidate")
        expected = (
            "ambiguous" if len(candidates) > 1 else "resolved" if candidates else "unresolved"
        )
        if row["status"] != expected:
            raise TypeScriptProtocolError("inconsistent status")
    diagnostics = value["diagnostics"]
    if not isinstance(diagnostics, list) or len(diagnostics) > 128:
        raise TypeScriptProtocolError("diagnostic limit")
    for item in diagnostics:
        if (
            not isinstance(item, dict)
            or not {"code", "message"} <= set(item)
            or any(not isinstance(item[k], str) or len(item[k]) > 4096 for k in ("code", "message"))
            or ("path" in item and not _path(item["path"]))
        ):
            raise TypeScriptProtocolError("invalid diagnostic")
    return value


class TypeScriptAdapter:
    """Run only inside the qualified analyzer container with Node 22 on PATH."""

    def resolve(self, input: dict[str, Any], budget: Any = None) -> dict[str, Any]:
        request = validate_input(input)
        entry = Path(__file__).resolve().parents[3] / "tools/typescript-analyzer/dist/src/cli.js"
        timeout = min(float(getattr(budget, "wall_seconds", 15)), 15)
        payload = json.dumps(request, ensure_ascii=False).encode()
        if len(payload) > 20 * 1024 * 1024:
            raise TypeScriptProtocolError("serialized input limit")
        # Temporary files bound retained process output; outer container supplies native limits.
        import tempfile

        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                completed = subprocess.run(
                    ["node", "--max-old-space-size=256", str(entry)],
                    input=payload,
                    stdout=output,
                    stderr=errors,
                    timeout=timeout,
                    cwd=entry.parent,
                    env={"PATH": "/usr/local/bin:/usr/bin:/bin", "NODE_OPTIONS": ""},
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise TypeScriptProtocolError("sidecar unavailable or timed out") from exc
            if completed.returncode:
                raise TypeScriptProtocolError("sidecar rejected input")
            output.seek(0)
            raw = output.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise TypeScriptProtocolError("output limit")
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise TypeScriptProtocolError("invalid JSON output") from exc
        return validate_result(result, request)
