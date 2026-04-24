"""Unit tests for ``RequestSpan``, status classification, and masking."""

from __future__ import annotations

import time

import pytest

from app.core.observability.span import (
    RequestSpan,
    UsageSnapshot,
    classify_status,
    mask_key,
    mask_uuid,
)


class _Usage:
    """Minimal stand-in for Anthropic's ``Usage`` Pydantic model."""

    def __init__(self, **kwargs):
        self._data = kwargs

    def model_dump(self):
        return dict(self._data)


def _make_span() -> RequestSpan:
    return RequestSpan.start(request_id="rid-1", method="POST", path="/v1/messages")


class TestClassifyStatus:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (0, "pending"),
            (200, "ok"),
            (201, "ok"),
            (301, "ok"),
            (400, "client_error"),
            (401, "auth_error"),
            (403, "auth_error"),
            (404, "client_error"),
            (418, "client_error"),
            (429, "rate_limited"),
            (499, "client_error"),
            (500, "upstream_error"),
            (502, "upstream_error"),
            (599, "upstream_error"),
        ],
    )
    def test_status_classification(self, code, expected):
        assert classify_status(code) == expected


class TestMasking:
    def test_mask_key_long(self):
        assert mask_key("sk-ant-abcdefghijklmnop") == "sk-ant…mnop"

    def test_mask_key_short(self):
        assert mask_key("short") == "***"

    def test_mask_key_threshold(self):
        # 12 chars → short branch; 13 chars → masked branch
        assert mask_key("a" * 12) == "***"
        assert mask_key("a" * 13) == "aaaaaa…aaaa"

    def test_mask_key_none(self):
        assert mask_key(None) is None
        assert mask_key("") == ""

    def test_mask_uuid(self):
        assert mask_uuid("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") == "aaaaaaaa…"
        assert mask_uuid(None) is None
        assert mask_uuid("") == ""


class TestRequestSpan:
    def test_start_initializes_timestamps(self):
        before = time.perf_counter()
        span = _make_span()
        after = time.perf_counter()
        assert before <= span.start_mono <= after
        assert span.status == "pending"
        assert span.http_status == 0

    def test_set_model(self):
        span = _make_span()
        span.set_model("claude-opus-4-7", stream=True)
        assert span.model == "claude-opus-4-7"
        assert span.stream is True

    def test_set_upstream(self):
        span = _make_span()
        span.set_upstream("oauth", account_id="org-123")
        assert span.upstream == "oauth"
        assert span.account_id == "org-123"

    def test_set_upstream_without_account_preserves_existing(self):
        span = _make_span()
        span.set_upstream("oauth", account_id="org-first")
        span.set_upstream("oauth")  # no account_id arg
        assert span.account_id == "org-first"

    def test_update_usage_from_model_dump(self):
        span = _make_span()
        usage = _Usage(
            input_tokens=100,
            output_tokens=42,
            cache_read_input_tokens=11,
            cache_creation_input_tokens=22,
        )
        span.update_usage(usage)
        assert span.usage == UsageSnapshot(
            input_tokens=100,
            output_tokens=42,
            cache_read_tokens=11,
            cache_write_tokens=22,
        )

    def test_update_usage_from_dict(self):
        span = _make_span()
        span.update_usage(
            {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 1}
        )
        assert span.usage.input_tokens == 7
        assert span.usage.output_tokens == 3
        assert span.usage.cache_read_tokens == 1
        assert span.usage.cache_write_tokens == 0

    def test_update_usage_partial(self):
        """Later updates overwrite per-field, not per-payload."""
        span = _make_span()
        span.update_usage({"input_tokens": 10, "output_tokens": 20})
        span.update_usage({"output_tokens": 99})
        assert span.usage.input_tokens == 10
        assert span.usage.output_tokens == 99

    def test_update_usage_none_and_junk(self):
        span = _make_span()
        span.update_usage(None)
        span.update_usage("not a usage")
        span.update_usage(42)
        assert span.usage == UsageSnapshot()

    def test_fail(self):
        span = _make_span()
        span.fail(RuntimeError("kaboom"))
        assert span.status == "exception"
        assert span.error == "RuntimeError"

    def test_finish_from_pending(self):
        span = _make_span()
        span.finish(200)
        assert span.http_status == 200
        assert span.status == "ok"

    def test_finish_does_not_override_exception(self):
        span = _make_span()
        span.fail(RuntimeError("x"))
        span.finish(500)
        assert span.status == "exception"  # fail wins
        assert span.http_status == 500

    def test_to_record_masks_account_id_and_client_key(self):
        span = _make_span()
        span.set_model("claude", stream=True)
        span.set_upstream("oauth", account_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        span.set_client_key("sk-ant-abcdefghijklmnop")
        span.update_usage({"input_tokens": 1, "output_tokens": 2})
        span.finish(200)
        record = span.to_record()
        assert record["event"] == "request.complete"
        assert record["account_id"] == "aaaaaaaa…"
        assert record["client_key"] == "sk-ant…mnop"
        assert record["http_status"] == 200
        assert record["input_tokens"] == 1
        assert isinstance(record["duration_ms"], int)
        assert record["duration_ms"] >= 0

    def test_client_key_absent_by_default(self):
        span = _make_span()
        assert span.client_key is None
        assert span.to_record()["client_key"] is None

    def test_to_record_pending_has_no_http_status(self):
        span = _make_span()
        record = span.to_record()
        assert record["http_status"] is None
