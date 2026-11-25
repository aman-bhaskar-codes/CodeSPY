"""find_symbol and search_semantic: the snapshot's indexes, with changed files flagged."""

from tests.agent.tools.support import (
    AWS,
    FILES,
    FakeSearch,
    FakeSymbols,
    SearchHit,
    SymbolHit,
    call,
    edit,
    make_context,
)


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


SYMBOLS = FakeSymbols(
    [
        SymbolHit("src/app.py", 1, "function", "main"),
        SymbolHit("src/app.py", 5, "function", "handle"),
        SymbolHit("src/util.py", 1, "variable", "main"),
        SymbolHit(".env", 1, "variable", "main"),
    ]
)


async def test_find_symbol_lists_definitions_with_evidence_and_hides_credential_files():
    ctx, _ = await make_context(symbols=SYMBOLS)
    result = await call(ctx, "find_symbol", name="main")
    assert result.content.splitlines() == [
        "function main  src/app.py:1",
        "variable main  src/util.py:1",
    ]
    assert [(e.provenance.path, e.provenance.start_line) for e in result.evidence] == [
        ("src/app.py", 1),
        ("src/util.py", 1),
    ]
    assert (
        await call(ctx, "find_symbol", name="main", kind="function")
    ).content == "function main  src/app.py:1"
    assert (await call(ctx, "find_symbol", name="missing")).content == "no definitions found"


async def test_find_symbol_flags_definitions_in_files_changed_this_session():
    ctx, _ = await make_context(symbols=SYMBOLS)
    await edit(ctx, "src/util.py", b"main = 2\n", sha(FILES["src/util.py"]))
    result = await call(ctx, "find_symbol", name="main")
    assert (
        "src/util.py:1  [changed in this session" in result.content
        and "src/app.py:1  [changed" not in result.content
    )


async def test_find_symbol_respects_the_limit_and_the_validation():
    ctx, _ = await make_context(symbols=SYMBOLS)
    assert (await call(ctx, "find_symbol", name="main", limit=1)).status == "partial"
    assert (await call(ctx, "find_symbol", name="main", limit=0)).status == "invalid"
    assert (await call(ctx, "find_symbol", name="")).status == "invalid"


async def test_search_semantic_returns_snippets_ranked_and_flags_stale_files():
    hits = [
        SearchHit("src/app.py", 1, 3, "def main():\n    return handle()", 0.91),
        SearchHit("src/util.py", 1, 1, "X = 1", 0.40),
        SearchHit(".env", 1, 1, f"SECRET={AWS}", 0.99),
    ]
    ctx, _ = await make_context(search=FakeSearch(hits))
    await edit(ctx, "src/util.py", b"X = 9\n", sha(FILES["src/util.py"]))
    result = await call(ctx, "search_semantic", query="where does the app start")
    assert result.content.startswith("src/app.py:1-3  (score 0.91)\ndef main():")
    assert "src/util.py:1-1  (score 0.40)  [changed in this session" in result.content
    assert ".env" not in result.content and AWS not in result.content
    assert len(result.evidence) == 2
    assert (await call(ctx, "search_semantic", query="x", k=1)).content.count("score") == 1


async def test_search_snippets_are_redacted():
    leaky = [SearchHit("src/config.py", 2, 2, f'AWS_KEY = "{AWS}"', 0.8)]
    ctx, _ = await make_context(search=FakeSearch(leaky))
    result = await call(ctx, "search_semantic", query="aws")
    assert AWS not in result.content and result.redactions == 1


async def test_without_the_services_the_tools_are_unavailable_not_empty():
    ctx, _ = await make_context()
    assert (await call(ctx, "find_symbol", name="main")).error_code == "no_symbol_index"
    assert (await call(ctx, "search_semantic", query="x")).error_code == "no_search_index"
