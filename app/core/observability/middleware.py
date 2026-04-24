"""Pure-ASGI middleware that owns the request lifecycle.

This runs outside FastAPI's ``Request`` abstraction on purpose: it lets
us intercept ``http.response.start`` (for the status code and to inject
the ``x-request-id`` header) and know that by the time the inner app
returns, the full response body has been sent to the server. No
``BaseHTTPMiddleware``, no wrapping ``body_iterator``, no dependency on
Starlette's internal ``_StreamingResponse`` shape.
"""

from __future__ import annotations

import json
import uuid
from typing import Awaitable, Callable, MutableMapping

from loguru import logger

from app.core.observability.context import _reset_span, _set_span
from app.core.observability.exporter import SpanExporter
from app.core.observability.span import RequestSpan


Scope = MutableMapping[str, object]
Message = MutableMapping[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestObservabilityMiddleware:
    """Start and finalize a ``RequestSpan`` for each traced HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        exporter: SpanExporter,
        trace_prefixes: tuple[str, ...] = ("/v1/",),
        request_id_header: str = "x-request-id",
    ):
        self.app = app
        self.exporter = exporter
        self.trace_prefixes = trace_prefixes
        self.request_id_header = request_id_header.lower()
        self._header_bytes = self.request_id_header.encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not self._should_trace(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        span = RequestSpan.start(
            request_id=request_id,
            method=str(scope.get("method") or ""),
            path=str(scope.get("path") or ""),
        )
        token = _set_span(span)
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = list(message.get("headers") or [])
                headers.append((self._header_bytes, request_id.encode("latin-1")))
                message["headers"] = headers
                status = int(message.get("status") or 0)
                span.finish(status)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            span.fail(exc)
            # FastAPI routes `Exception` to ServerErrorMiddleware (outside
            # user middleware), so letting the exception propagate would
            # bypass send_wrapper and lose x-request-id. Instead we log
            # ourselves and send a JSON 500 through our wrapper.
            logger.exception(
                f"Unhandled exception in {span.method} {span.path}: "
                f"{type(exc).__name__}: {exc}"
            )
            if not response_started:
                await self._send_error_response(send_wrapper)
            # Swallow: we've produced a response; re-raising would trigger
            # ServerErrorMiddleware to try sending another one.
        finally:
            self._safe_export(span)
            _reset_span(token)

    @staticmethod
    async def _send_error_response(send: Send) -> None:
        body = json.dumps(
            {
                "detail": {
                    "code": 500000,
                    "message": "Internal Server Error",
                }
            }
        ).encode("utf-8")
        try:
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
        except Exception as exc:
            logger.debug(f"Failed to send error response: {exc!r}")

    def _should_trace(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        return any(path.startswith(p) for p in self.trace_prefixes)

    def _resolve_request_id(self, scope: Scope) -> str:
        headers = scope.get("headers") or ()
        for name, value in headers:
            if name == self._header_bytes:
                try:
                    return value.decode("latin-1")
                except Exception:
                    break
        return uuid.uuid4().hex

    def _safe_export(self, span: RequestSpan) -> None:
        try:
            self.exporter.export(span)
        except Exception as exc:
            logger.warning(f"Span exporter raised: {exc!r}")


def build_default_exporter() -> SpanExporter:
    """Construct the default exporter from current settings.

    Reads ``access_log_sample_rate_*`` to wrap the Loguru exporter in a
    ``SampledExporter`` when any sample rate is below 1.0.
    """
    from app.core.config import settings
    from app.core.observability.exporter import LoguruExporter, SampledExporter

    exporter: SpanExporter = LoguruExporter()
    ok_rate = settings.access_log_sample_rate_ok
    err_rate = settings.access_log_sample_rate_error
    if ok_rate < 1.0 or err_rate < 1.0:
        exporter = SampledExporter(exporter, ok_rate=ok_rate, error_rate=err_rate)
    return exporter


__all__ = [
    "RequestObservabilityMiddleware",
    "build_default_exporter",
]
