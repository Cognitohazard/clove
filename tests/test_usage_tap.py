"""Unit tests for the OAuth usage observation taps."""

from __future__ import annotations

import asyncio
import json

from app.core.observability import RequestSpan
from app.core.observability.usage_tap import (
    JSONUsageTap,
    NullUsageTap,
    SSEUsageTap,
    create_usage_tap,
)


def _span() -> RequestSpan:
    return RequestSpan.start(request_id="r", method="POST", path="/v1/messages")


class TestNullUsageTap:
    async def test_no_op(self):
        tap = NullUsageTap()
        tap.feed(b"anything")
        await tap.close()


class TestJSONUsageTap:
    async def test_captures_usage_from_full_body(self):
        span = _span()
        tap = JSONUsageTap(span)
        body = json.dumps(
            {
                "id": "m",
                "usage": {
                    "input_tokens": 55,
                    "output_tokens": 77,
                    "cache_read_input_tokens": 3,
                    "cache_creation_input_tokens": 4,
                },
            }
        ).encode()
        tap.feed(body)
        await tap.close()
        assert span.usage.input_tokens == 55
        assert span.usage.output_tokens == 77
        assert span.usage.cache_read_tokens == 3
        assert span.usage.cache_write_tokens == 4

    async def test_handles_split_chunks(self):
        span = _span()
        tap = JSONUsageTap(span)
        body = b'{"usage":{"input_tokens":1,"output_tokens":2}}'
        tap.feed(body[:10])
        tap.feed(body[10:25])
        tap.feed(body[25:])
        await tap.close()
        assert span.usage.input_tokens == 1
        assert span.usage.output_tokens == 2

    async def test_ignores_malformed_body(self):
        span = _span()
        tap = JSONUsageTap(span)
        tap.feed(b"{not json")
        await tap.close()
        assert span.usage.input_tokens == 0

    async def test_ignores_missing_usage(self):
        span = _span()
        tap = JSONUsageTap(span)
        tap.feed(b'{"id": "m"}')
        await tap.close()
        assert span.usage.input_tokens == 0


class TestSSEUsageTap:
    async def test_captures_usage_across_chunks(self):
        span = _span()
        tap = SSEUsageTap(span)
        start = (
            b"event: message_start\ndata: "
            b'{"type":"message_start","message":{"id":"m","type":"message",'
            b'"role":"assistant","model":"claude","content":[],'
            b'"stop_reason":null,"stop_sequence":null,'
            b'"usage":{"input_tokens":123,"output_tokens":0,'
            b'"cache_read_input_tokens":11,"cache_creation_input_tokens":22}}}'
            b"\n\n"
        )
        delta = (
            b"event: message_delta\ndata: "
            b'{"type":"message_delta","delta":{"stop_reason":"end_turn",'
            b'"stop_sequence":null},'
            b'"usage":{"input_tokens":123,"output_tokens":99,'
            b'"cache_creation_input_tokens":22,"cache_read_input_tokens":11}}'
            b"\n\n"
        )
        # Split mid-event to verify cross-chunk buffering inside EventParser.
        tap.feed(start[:40])
        tap.feed(start[40:])
        tap.feed(delta)
        await tap.close()
        assert span.usage.input_tokens == 123
        assert span.usage.output_tokens == 99
        assert span.usage.cache_read_tokens == 11
        assert span.usage.cache_write_tokens == 22

    async def test_malformed_events_ignored(self):
        span = _span()
        tap = SSEUsageTap(span)
        tap.feed(b"event: message_delta\ndata: not-json\n\n")
        await tap.close()
        # No crash, no updates.
        assert span.usage.input_tokens == 0

    async def test_close_is_idempotent_under_cancellation(self):
        """Closing cancels the background task cleanly even if nothing was fed."""
        span = _span()
        tap = SSEUsageTap(span)
        await asyncio.wait_for(tap.close(), timeout=2.0)


class TestFactory:
    def test_dispatches_on_content_type(self):
        span = _span()
        assert isinstance(create_usage_tap(span, "text/event-stream"), SSEUsageTap)
        assert isinstance(
            create_usage_tap(span, "text/event-stream; charset=utf-8"),
            SSEUsageTap,
        )
        assert isinstance(create_usage_tap(span, "application/json"), JSONUsageTap)
        assert isinstance(create_usage_tap(span, ""), JSONUsageTap)
        assert isinstance(create_usage_tap(None, "text/event-stream"), NullUsageTap)
