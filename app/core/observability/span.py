"""The ``RequestSpan`` — one observability record per HTTP request.

The span is a plain dataclass written by many collaborators (middleware,
pipeline processors, services) and read once by an exporter at the end
of the request. It intentionally mirrors OpenTelemetry span semantics
so an OTel exporter can be added later without touching anything else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional


Status = Literal[
    "pending",
    "ok",
    "client_error",
    "rate_limited",
    "auth_error",
    "upstream_error",
    "exception",
]


def classify_status(http_status: int) -> Status:
    """Map an HTTP status code to a span status.

    Kept in one place so sinks, dashboards, and tests agree on the taxonomy.
    """
    if http_status == 0:
        return "pending"
    if http_status >= 500:
        return "upstream_error"
    if http_status == 429:
        return "rate_limited"
    if http_status in (401, 403):
        return "auth_error"
    if http_status >= 400:
        return "client_error"
    return "ok"


def mask_key(value: Optional[str]) -> Optional[str]:
    """Mask a secret-bearing string for log output.

    Preserves a short prefix/suffix so operators can match against records
    without exposing enough to reuse the credential.
    """
    if not value:
        return value
    if len(value) <= 12:
        return "***"
    return f"{value[:6]}…{value[-4:]}"


def mask_uuid(value: Optional[str]) -> Optional[str]:
    """Mask a UUID-like identifier down to its first 8 characters."""
    if not value:
        return value
    return f"{value[:8]}…"


@dataclass
class UsageSnapshot:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class RequestSpan:
    request_id: str
    method: str
    path: str
    start_mono: float

    # Attributes written during pipeline processing.
    model: Optional[str] = None
    stream: bool = False
    upstream: Optional[Literal["oauth", "web"]] = None
    account_id: Optional[str] = None
    client_key: Optional[str] = None  # raw; masked at to_record()
    usage: UsageSnapshot = field(default_factory=UsageSnapshot)

    # Terminal state, written by middleware.
    status: Status = "pending"
    http_status: int = 0
    error: Optional[str] = None

    @classmethod
    def start(cls, *, request_id: str, method: str, path: str) -> "RequestSpan":
        return cls(
            request_id=request_id,
            method=method,
            path=path,
            start_mono=time.perf_counter(),
        )

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def set_model(self, model: Optional[str], *, stream: bool = False) -> None:
        self.model = model
        self.stream = stream

    def set_upstream(
        self,
        upstream: Literal["oauth", "web"],
        *,
        account_id: Optional[str] = None,
    ) -> None:
        self.upstream = upstream
        if account_id is not None:
            self.account_id = account_id

    def set_client_key(self, api_key: Optional[str]) -> None:
        self.client_key = api_key

    def update_usage(self, source: Any) -> None:
        """Copy usage fields off an Anthropic ``Usage`` model or a plain dict."""
        if source is None:
            return
        if hasattr(source, "model_dump"):
            data = source.model_dump()
        elif isinstance(source, dict):
            data = source
        else:
            return
        mapping = (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cache_read_input_tokens"),
            ("cache_write_tokens", "cache_creation_input_tokens"),
        )
        for dst, src in mapping:
            value = data.get(src)
            if value is not None:
                setattr(self.usage, dst, int(value) or 0)

    def fail(self, exc: BaseException) -> None:
        self.status = "exception"
        self.error = type(exc).__name__

    def finish(self, http_status: int) -> None:
        self.http_status = http_status
        if self.status == "pending":
            self.status = classify_status(http_status)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def duration_ms(self) -> int:
        return int((time.perf_counter() - self.start_mono) * 1000)

    def to_record(self) -> dict:
        """Materialize the span as a log-ready dict with sensitive fields masked."""
        return {
            "event": "request.complete",
            "request_id": self.request_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "http_status": self.http_status or None,
            "duration_ms": self.duration_ms(),
            "model": self.model,
            "stream": self.stream,
            "upstream": self.upstream,
            "account_id": mask_uuid(self.account_id),
            "client_key": mask_key(self.client_key),
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "error": self.error,
        }
