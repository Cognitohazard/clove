"""Upstream-body taps that copy usage into the current ``RequestSpan``.

Used on the OAuth passthrough path where we cannot parse → re-serialize
(byte transparency). The tap sits in parallel with the pass-through
stream: each chunk is fed to the tap *and* yielded to the client
unchanged.

Three implementations cover every upstream shape:

* ``SSEUsageTap`` — streaming responses (``text/event-stream``). Reuses
  the canonical ``EventParser`` from ``app.services.event_processing``
  via a background task fed by a queue, so the pass-through yield
  cadence is unaffected and parsing runs on the one tested parser.
* ``JSONUsageTap`` — non-streaming responses (``application/json``).
  Buffers bytes and parses once at close.
* ``NullUsageTap`` — no-op used when no span is active (e.g. untraced
  paths, unit tests), so callers get a single uniform interface.

Observation must never crash a request: exceptions during parsing are
logged at debug and swallowed.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

from loguru import logger

from app.core.observability.span import RequestSpan
from app.models.streaming import (
    MessageDeltaEvent,
    MessageStartEvent,
    StreamingEvent,
)
from app.services.event_processing.event_parser import EventParser


@runtime_checkable
class UsageTap(Protocol):
    def feed(self, chunk: bytes) -> None: ...

    async def close(self) -> None: ...


class NullUsageTap:
    def feed(self, chunk: bytes) -> None:
        return

    async def close(self) -> None:
        return


class SSEUsageTap:
    """Parse SSE events in a background task; write usage onto the span.

    The consumer task is created lazily on the first ``feed`` call so
    construction is safe from synchronous code and zero-chunk requests
    cost nothing.
    """

    def __init__(self, span: RequestSpan):
        self._span = span
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def feed(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", errors="replace")
        if self._task is None:
            self._task = asyncio.create_task(self._consume())
        self._queue.put_nowait(text)

    async def close(self) -> None:
        if self._task is None:
            return
        self._queue.put_nowait(None)
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        except Exception as exc:
            logger.debug(f"SSE usage tap join error: {exc!r}")

    async def _consume(self) -> None:
        try:
            parser = EventParser(skip_unknown_events=True)
            async for event in parser.parse_stream(self._iter()):
                self._observe(event)
        except Exception as exc:
            logger.debug(f"SSE usage tap parse error: {exc!r}")

    async def _iter(self) -> AsyncIterator[str]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    def _observe(self, event: StreamingEvent) -> None:
        root = event.root
        if isinstance(root, MessageStartEvent):
            self._span.update_usage(getattr(root.message, "usage", None))
        elif isinstance(root, MessageDeltaEvent):
            self._span.update_usage(getattr(root, "usage", None))


class JSONUsageTap:
    """Buffer a non-streaming body and extract ``usage`` at close."""

    def __init__(self, span: RequestSpan):
        self._span = span
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def close(self) -> None:
        if not self._buffer:
            return
        try:
            payload = json.loads(bytes(self._buffer))
        except ValueError:
            return
        if isinstance(payload, dict):
            self._span.update_usage(payload.get("usage"))


def create_usage_tap(span: Optional[RequestSpan], content_type: str) -> UsageTap:
    """Pick the right tap for the upstream response shape."""
    if span is None:
        return NullUsageTap()
    if "text/event-stream" in (content_type or "").lower():
        return SSEUsageTap(span)
    return JSONUsageTap(span)
