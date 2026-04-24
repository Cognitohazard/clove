"""Integration tests for ``RequestObservabilityMiddleware``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.observability import (
    RequestObservabilityMiddleware,
    RequestSpan,
    current_span,
)


@dataclass
class _Recorder:
    seen: List[RequestSpan] = field(default_factory=list)

    def export(self, span: RequestSpan) -> None:
        self.seen.append(span)


def _build_app(recorder: _Recorder) -> TestClient:
    async def ok(request):
        span = current_span()
        assert span is not None
        span.set_model("m1", stream=False)
        span.set_upstream("oauth", account_id="org-abc")
        span.update_usage({"input_tokens": 10, "output_tokens": 5})
        return JSONResponse({"ok": True})

    async def stream(request):
        span = current_span()

        async def gen():
            yield b"hello"
            span.update_usage({"output_tokens": 7})
            yield b"world"

        return StreamingResponse(gen())

    async def boom(request):
        raise RuntimeError("kaboom")

    async def four_oh_four(request):
        return JSONResponse({"e": "nope"}, status_code=404)

    async def rate_limited(request):
        return JSONResponse({"e": "slow"}, status_code=429)

    async def auth_error(request):
        return JSONResponse({"e": "nope"}, status_code=401)

    async def upstream_error(request):
        return JSONResponse({"e": "bad"}, status_code=502)

    async def admin(request):
        # Outside the /v1/ prefix; must not be traced.
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/v1/ok", ok),
            Route("/v1/stream", stream),
            Route("/v1/boom", boom),
            Route("/v1/notfound", four_oh_four),
            Route("/v1/ratelimited", rate_limited),
            Route("/v1/autherror", auth_error),
            Route("/v1/upstream", upstream_error),
            Route("/api/admin/accounts", admin),
        ]
    )
    app.add_middleware(RequestObservabilityMiddleware, exporter=recorder)
    return TestClient(app, raise_server_exceptions=False)


class TestTracedPaths:
    def test_ok_exports_span_with_attributes(self):
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get("/v1/ok")
        assert r.status_code == 200
        assert r.headers["x-request-id"]
        assert len(rec.seen) == 1
        s = rec.seen[0]
        assert s.status == "ok"
        assert s.http_status == 200
        assert s.model == "m1"
        assert s.upstream == "oauth"
        assert s.account_id == "org-abc"  # raw on span; masked at to_record
        assert s.usage.input_tokens == 10
        assert s.usage.output_tokens == 5

    def test_streaming_emission_happens_after_body(self):
        """Usage written while streaming must appear in the exported span."""
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get("/v1/stream")
        assert r.content == b"helloworld"
        s = rec.seen[0]
        assert s.usage.output_tokens == 7  # set between yields

    def test_inbound_request_id_is_honored(self):
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get("/v1/ok", headers={"x-request-id": "caller-supplied-123"})
        assert r.headers["x-request-id"] == "caller-supplied-123"
        assert rec.seen[0].request_id == "caller-supplied-123"

    @pytest.mark.parametrize(
        "path,expected_status,expected_http",
        [
            ("/v1/notfound", "client_error", 404),
            ("/v1/ratelimited", "rate_limited", 429),
            ("/v1/autherror", "auth_error", 401),
            ("/v1/upstream", "upstream_error", 502),
        ],
    )
    def test_status_taxonomy(self, path, expected_status, expected_http):
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get(path)
        assert r.status_code == expected_http
        assert rec.seen[0].status == expected_status

    def test_exception_path(self):
        rec = _Recorder()
        client = _build_app(rec)
        client.get("/v1/boom")
        s = rec.seen[0]
        assert s.status == "exception"
        assert s.error == "RuntimeError"

    def test_unhandled_exception_gets_500_with_request_id(self):
        """Unhandled exceptions in traced routes produce a 500 JSON response
        whose x-request-id header is the caller-supplied id.

        The middleware must handle the exception itself because FastAPI
        routes ``Exception`` to ``ServerErrorMiddleware`` (outside user
        middleware), which would bypass our header injection.
        """
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get("/v1/boom", headers={"x-request-id": "caller-err"})
        assert r.status_code == 500
        assert r.headers.get("x-request-id") == "caller-err"
        assert r.json()["detail"]["code"] == 500000
        s = rec.seen[0]
        assert s.status == "exception"
        assert s.error == "RuntimeError"


class TestUntracedPaths:
    def test_admin_path_not_traced(self):
        rec = _Recorder()
        client = _build_app(rec)
        r = client.get("/api/admin/accounts")
        assert r.status_code == 200
        assert rec.seen == []
        # Untraced paths do not inject x-request-id.
        assert "x-request-id" not in r.headers


class TestExactlyOnce:
    def test_one_export_per_request(self):
        rec = _Recorder()
        client = _build_app(rec)
        for _ in range(5):
            client.get("/v1/ok")
        assert len(rec.seen) == 5

    def test_exporter_failure_does_not_crash_request(self):
        class Raiser:
            def export(self, span):
                raise RuntimeError("exporter boom")

        async def ok(request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/v1/ok", ok)])
        app.add_middleware(RequestObservabilityMiddleware, exporter=Raiser())
        client = TestClient(app)
        r = client.get("/v1/ok")
        assert r.status_code == 200  # did not crash
