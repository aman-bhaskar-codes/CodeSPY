"""Self-labelled static adapter and service-link tasks; no runtime-flow claims."""

import hashlib
from uuid import uuid4

from app.analyzers.frameworks.express import observe_express
from app.analyzers.frameworks.fastapi import observe_fastapi
from app.analyzers.frameworks.http_client import observe_http
from app.analyzers.frameworks.matching import match_boundaries
from app.contracts.structure import AnalysisConfigV1, ServiceBindingV1, SourceUnitV1


def source(path, text, language):
    raw = text.encode()
    return SourceUnitV1(
        file_id=uuid4(),
        path=path,
        data=raw,
        language=language,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_labelled_framework_and_candidate_link_thresholds():
    config = AnalysisConfigV1(
        toolchain_hash="a" * 64,
        service_bindings=(
            ServiceBindingV1(
                source_prefix="api", service_key="api", canonical_origin="https://api.test"
            ),
            ServiceBindingV1(
                source_prefix="web",
                service_key="web",
                canonical_origin="https://web.test",
                same_origin_service="api",
            ),
        ),
    )
    for adapter in ("fastapi", "express", "http_client"):
        tp = fp = fn = 0
        for index in range(40):
            supported = index < 20
            path = f"/v1/items/{index}"
            if adapter == "express":
                backend = source(
                    "api/server.js",
                    'import express from "express"; const app=express(); '
                    "const router=express.Router(); "
                    + (
                        f'router.get("/items/{index}", handler);'
                        if supported
                        else "router.get(dynamicPath, handler);"
                    )
                    + 'app.use("/v1",router);',
                    "javascript",
                )
                routes, _ = observe_express((backend,), (), config)
            else:
                backend = source(
                    "api/server.py",
                    "from fastapi import FastAPI\napp=FastAPI()\n"
                    + (
                        f'@app.get("{path}")\ndef handle(): pass\n'
                        if supported or adapter == "http_client"
                        else "@app.get(dynamic_path)\ndef handle(): pass\n"
                    ),
                    "python",
                )
                routes, _ = observe_fastapi((backend,), (), config)
            client_text = f'fetch("{path}");'
            if adapter == "http_client" and not supported:
                client_text = f'fetch("{path}", dynamicOptions);'
            client = source("web/client.js", client_text, "javascript")
            requests, _ = observe_http((client,), (), config)
            links = match_boundaries(routes, requests, config.service_bindings)
            correct = (
                bool(links) and links[0].state == "candidate" and len(links[0].route_keys) == 1
            )
            if supported:
                tp += int(correct)
                fn += int(not correct)
            else:
                fp += int(correct)
        precision, recall = tp / (tp + fp) if tp + fp else 0, tp / (tp + fn)
        print(
            {
                "adapter": adapter,
                "supported": 20,
                "negative": 20,
                "tp": tp,
                "fp": fp,
                "fn": fn,
                "precision": precision,
                "recall": recall,
            }
        )
        assert precision >= 0.95 and recall >= 0.80 and fp == 0
