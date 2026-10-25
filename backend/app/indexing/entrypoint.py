"""Fixed offline index protocol; input content never becomes commands or diagnostics."""

import hashlib
import os
import sys
from pathlib import Path

import tiktoken

from app.contracts.indexing import TOKENIZER_SHA256, IndexSpecV1
from app.domain.canonical_json import canonical_bytes, load_json
from app.indexing.materialize import IndexMaterializationError, assemble_draft, materialize_file
from app.ports.index_builder import IndexCapabilitiesV1
from app.source_read.entrypoint import read_fixed

VOCAB_FILE = "9b5ad71b2ce5302211f9c61530b329a4922fc6a4"


def offline_tokenizer(cache: Path) -> tiktoken.Encoding:
    # Check before get_encoding, whose normal cache-miss behavior may fetch data.
    raw = read_fixed(cache, VOCAB_FILE, 2_097_152)
    if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError("index_tokenizer_unavailable")
    if os.environ.get("TIKTOKEN_CACHE_DIR") != str(cache):
        raise ValueError("index_tokenizer_unavailable")
    return tiktoken.get_encoding("cl100k_base")


def execute(root: Path, source_hash: str, cache: Path) -> bytes:
    try:
        return _execute(root, source_hash, cache)
    except (ValueError, OSError, IndexMaterializationError):
        raise ValueError("index_input_invalid") from None


def _execute(root: Path, source_hash: str, cache: Path) -> bytes:
    capability = IndexCapabilitiesV1(runtime_source_sha256=source_hash, max_request_bytes=98_368)
    encoder = offline_tokenizer(cache)
    request = load_json(read_fixed(root, "request.json", 98_368).decode())
    if request == {"operation": "capabilities"}:
        return canonical_bytes(capability.model_dump())
    if not isinstance(request, dict) or set(request) != {"operation", "spec"}:
        raise ValueError("index_request_invalid")
    if request["operation"] != "build":
        raise ValueError("index_request_invalid")
    spec = IndexSpecV1.model_validate_json(canonical_bytes(request["spec"]))
    if spec.config.runtime_source_sha256 != source_hash:
        raise ValueError("index_runtime_differs")
    manifest = load_json(read_fixed(root, "manifest.json", 65_536).decode())
    if not isinstance(manifest, dict) or set(manifest) != {"files"}:
        raise ValueError("index_input_invalid")
    rows = manifest["files"]
    if not isinstance(rows, list) or len(rows) != len(spec.files):
        raise ValueError("index_input_invalid")
    source = read_fixed(root, "archive.zip", 16_777_216)
    offset = 0
    results = []
    for file, row in zip(spec.files, rows, strict=True):
        expected = {"file_id": str(file.file_id), "offset": offset, "size_bytes": file.size_bytes}
        if row != expected:
            raise ValueError("index_input_invalid")
        end = offset + file.size_bytes
        results.append(materialize_file(spec, file, source[offset:end], tokenizer=encoder))
        offset = end
    if offset != len(source):
        raise ValueError("index_input_invalid")
    draft = assemble_draft(spec, results)
    output = canonical_bytes(draft.model_dump())
    if len(output) > 25_165_824:
        raise ValueError("index_output_invalid")
    return output


def main() -> int:
    try:
        identity = load_json(read_fixed(Path("/app"), "runtime.json", 1024).decode())
        if not isinstance(identity, dict) or set(identity) != {"runtime_source_sha256"}:
            return 65
        output = execute(Path("/input"), identity["runtime_source_sha256"], Path("/opt/tokenizer"))
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 65


if __name__ == "__main__":
    raise SystemExit(main())
