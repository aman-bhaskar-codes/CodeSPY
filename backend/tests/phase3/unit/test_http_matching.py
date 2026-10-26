from app.analyzers.frameworks.fastapi import observe_fastapi
from app.analyzers.frameworks.http_client import observe_http
from app.analyzers.frameworks.matching import match_boundaries
from app.contracts.structure import AnalysisConfigV1, ServiceBindingV1

from tests.phase3.unit.test_fastapi import unit


def test_relative_requires_explicit_reviewed_binding():
    bindings = (
        ServiceBindingV1(
            source_prefix="api", service_key="api", canonical_origin="https://api.test"
        ),
        ServiceBindingV1(
            source_prefix="web",
            service_key="web",
            canonical_origin="https://web.test",
            same_origin_service="api",
        ),
    )
    config = AnalysisConfigV1(toolchain_hash="0" * 64, service_bindings=bindings)
    routes, _ = observe_fastapi(
        (unit('from fastapi import FastAPI\napp=FastAPI()\n@app.get("/x")\ndef x(): pass'),),
        (),
        config,
    )
    requests, _ = observe_http((unit('fetch("/x")', "javascript", "web/main.js"),), (), config)
    assert match_boundaries(routes, requests, bindings)[0].state == "candidate"
    requests, _ = observe_http((unit('fetch("/x")', "javascript", "other/main.js"),), (), config)
    assert match_boundaries(routes, requests, bindings)[0].state == "unmatched"
