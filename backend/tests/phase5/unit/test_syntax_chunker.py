"""Unit tests for syntax-aware code chunker, token budgeting, and windowing."""

from uuid import uuid4

from app.contracts.chunking import ChunkKind, ChunkProfileV1
from app.services.syntax_chunker import (
    SyntaxAwareChunker,
    count_tokens,
)


def test_count_tokens_tiktoken() -> None:
    code = "def process_data(items: list[str]) -> int:\n    return len(items)\n"
    tokens = count_tokens(code, "cl100k_base")
    assert tokens > 0
    assert tokens < 30


def test_small_file_single_chunk() -> None:
    chunker = SyntaxAwareChunker(ChunkProfileV1(target_tokens=500, max_tokens=1000))
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    code = b"print('hello world')\n"
    chunks = chunker.chunk_file(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=file_id,
        file_path="src/main.py",
        raw_content=code,
        cpg_nodes=None,
    )

    assert len(chunks) == 1
    c = chunks[0]
    assert c.chunk_kind == ChunkKind.FILE
    assert c.start_byte == 0
    assert c.end_byte == len(code)
    assert c.start_line == 1
    assert c.end_line == 1
    assert c.raw_content == code


def test_cpg_syntax_aware_chunking() -> None:
    chunker = SyntaxAwareChunker()
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    code = (
        b"# Module Header\n\n"
        b"class AccountService:\n"
        b"    def get_balance(self) -> int:\n"
        b"        return 100\n\n"
        b"def global_helper():\n"
        b"    return 'ok'\n"
    )

    # Mock CPG nodes for class, method, function
    cls_start = code.find(b"class AccountService:")
    cls_end = code.find(b"def global_helper():")
    meth_start = code.find(b"def get_balance")
    meth_end = cls_end
    func_start = cls_end
    func_end = len(code)

    cpg_nodes = [
        {
            "local_id": uuid4(),
            "node_kind": "class",
            "symbol_name": "AccountService",
            "qualified_name": "src.service.AccountService",
            "start_byte": cls_start,
            "end_byte": cls_end,
        },
        {
            "local_id": uuid4(),
            "node_kind": "method",
            "symbol_name": "get_balance",
            "qualified_name": "src.service.AccountService.get_balance",
            "start_byte": meth_start,
            "end_byte": meth_end,
        },
        {
            "local_id": uuid4(),
            "node_kind": "function",
            "symbol_name": "global_helper",
            "qualified_name": "src.service.global_helper",
            "start_byte": func_start,
            "end_byte": func_end,
        },
    ]

    chunks = chunker.chunk_file(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=file_id,
        file_path="src/service.py",
        raw_content=code,
        cpg_nodes=cpg_nodes,
    )

    # Must preserve symbols and qualified names
    symbols = {c.symbol_name for c in chunks if c.symbol_name}
    assert "AccountService" in symbols
    assert "get_balance" in symbols
    assert "global_helper" in symbols

    for c in chunks:
        assert c.start_byte < c.end_byte
        assert c.raw_content == code[c.start_byte : c.end_byte]
        assert c.start_line <= c.end_line


def test_oversized_function_splits_into_windows() -> None:
    """Test that a function exceeding token bound splits into newline-aligned windows."""
    chunker = SyntaxAwareChunker(
        ChunkProfileV1(
            target_tokens=64,
            max_tokens=100,
            max_bytes=300,
            overlap_tokens=10,
        )
    )
    org_id = uuid4()
    repo_id = uuid4()
    snap_id = uuid4()
    file_id = uuid4()

    # Generate a large function
    lines = [b"def massive_function():\n"]
    for i in range(50):
        lines.append(f"    item_{i} = do_complex_calculation({i})\n".encode())
    code = b"".join(lines)

    cpg_nodes = [
        {
            "local_id": uuid4(),
            "node_kind": "function",
            "symbol_name": "massive_function",
            "qualified_name": "module.massive_function",
            "start_byte": 0,
            "end_byte": len(code),
        }
    ]

    chunks = chunker.chunk_file(
        org_id=org_id,
        repository_id=repo_id,
        snapshot_id=snap_id,
        file_id=file_id,
        file_path="src/large.py",
        raw_content=code,
        cpg_nodes=cpg_nodes,
    )

    assert len(chunks) > 1
    for c in chunks:
        assert c.chunk_kind == ChunkKind.WINDOW
        assert c.symbol_name == "massive_function"
        assert c.token_count <= 80
        assert c.byte_count <= 400
        # Check window alignment: chunk starts and ends at line boundaries
        assert c.raw_content.endswith(b"\n")
