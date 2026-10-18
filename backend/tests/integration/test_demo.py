from scripts.phase1_demo import demonstrate


async def test_demo_assertions(factory):
    result = await demonstrate(factory)
    assert result == {
        "nodes": 2,
        "edges": 1,
        "evidence": 2,
        "derivations": 1,
        "replay": True,
        "rollback": True,
        "scope_rejection": True,
        "stale_rejection": True,
    }
