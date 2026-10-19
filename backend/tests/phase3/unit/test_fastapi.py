from hashlib import sha256
from uuid import UUID

import pytest
from app.analyzers.frameworks.fastapi import observe_fastapi
from app.contracts.structure import AnalysisConfigV1, SourceUnitV1


def unit(source, language="python", path="api/main.py"):
    data = source.encode()
    return SourceUnitV1(
        file_id=UUID(int=1),
        path=path,
        sha256=sha256(data).hexdigest(),
        data=data,
        language=language,
    )


def config():
    return AnalysisConfigV1(toolchain_hash="0" * 64)


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
@pytest.mark.parametrize("factory", ["FastAPI", "APIRouter"])
def test_literal_decorators(method, factory):
    source = (
        f"from fastapi import {factory} as Factory\n"
        "app = Factory()\n"
        f'@app.{method}("/items")\n'
        "def handler(): pass\n"
    )
    routes, diagnostics = observe_fastapi((unit(source),), (), config())
    assert [(r.method, r.template_path) for r in routes] == [(method.upper(), "/items")]
    assert diagnostics == ()
    assert routes[0].registration.range.start_line == 3


def test_repeated_nested_mounts_preserve_occurrences():
    source = """from fastapi import FastAPI, APIRouter
app = FastAPI()
r = APIRouter(prefix='/v1')
@app.get('/health')
def health(): pass
@r.get('/items')
def items(): pass
app.include_router(r, prefix='/a')
app.include_router(r, prefix='/b')
"""
    routes, _ = observe_fastapi((unit(source),), (), config())
    assert sorted(r.template_path for r in routes) == ["/a/v1/items", "/b/v1/items", "/health"]
    assert len({r.key for r in routes}) == 3


@pytest.mark.parametrize("expression", ["dynamic", 'f"/{x}"', '"/" + value', "factory()", "None"])
def test_dynamic_path_abstains(expression):
    source = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        f"@app.get({expression})\n"
        "def handler(): pass\n"
    )
    routes, diagnostics = observe_fastapi((unit(source),), (), config())
    assert all(r.state == "unresolved" and r.template_path is None for r in routes)
    assert diagnostics


@pytest.mark.parametrize("prefix", ["value", "factory()", "None", 'f"/{x}"'])
def test_dynamic_mount_never_falls_back_to_unmounted_route(prefix):
    source = (
        "from fastapi import FastAPI, APIRouter\n"
        "app=FastAPI()\n"
        "r=APIRouter()\n"
        '@r.get("/x")\n'
        "def x(): pass\n"
        f"app.include_router(r,prefix={prefix})"
    )
    routes, diagnostics = observe_fastapi((unit(source),), (), config())
    assert not any(r.state == "resolved" for r in routes)
    assert diagnostics
