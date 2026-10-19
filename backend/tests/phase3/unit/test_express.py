import pytest
from app.analyzers.frameworks.express import observe_express
from app.analyzers.frameworks.http_client import observe_http
from tests.phase3.unit.test_fastapi import config, unit


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
@pytest.mark.parametrize(
    "factory",
    [
        "const express = require('express');",
        "import express from 'express';",
        "import ex from 'express'; const express=ex;",
    ],
)
def test_express_literal_routes(method, factory):
    routes, diagnostics = observe_express(
        (
            unit(
                f'{factory} const app=express(); app.{method}("/x", [auth, handler]);', "javascript"
            ),
        ),
        (),
        config(),
    )
    assert [(r.method, r.template_path) for r in routes] == [(method.upper(), "/x")]
    assert "middleware_order_unverified" in routes[0].limitations
    assert diagnostics == ()


@pytest.mark.parametrize(
    "path", ['"/x*"', '"/x?"', '"/x(foo)"', "/abc/", "`/${x}`", "dynamic", "null"]
)
@pytest.mark.parametrize("method", ["get", "post", "use"])
def test_express_unsupported_path_abstains(path, method):
    routes, diagnostics = observe_express(
        (
            unit(
                'const express=require("express"); const app=express(); '
                f"app.{method}({path}, handler);",
                "javascript",
            ),
        ),
        (),
        config(),
    )
    assert not any(r.state == "resolved" for r in routes)
    assert diagnostics


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "head", "options"])
@pytest.mark.parametrize("client", ["fetch", "axios"])
def test_http_static_methods(method, client):
    source = (
        f'fetch("https://example.test/x?q=1", {{method:"{method.upper()}"}})'
        if client == "fetch"
        else f'import axios from "axios"; axios.{method}("https://example.test/x?q=1");'
    )
    requests, diagnostics = observe_http((unit(source, "javascript"),), (), config())
    assert [(r.method, r.path, r.origin_state) for r in requests] == [
        (method.upper(), "/x", "known")
    ]
    assert diagnostics == ()


@pytest.mark.parametrize("url", ["dynamic", "`/${x}`", 'new Request("/x")', "config", "null"])
@pytest.mark.parametrize("client", ["fetch", "axios"])
def test_http_dynamic_url_unresolved(url, client):
    source = (
        f"fetch({url})" if client == "fetch" else f'import axios from "axios"; axios.get({url})'
    )
    requests, diagnostics = observe_http((unit(source, "javascript"),), (), config())
    assert requests and all(r.origin_state == "unknown" for r in requests)
    assert diagnostics
