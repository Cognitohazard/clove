"""Unit tests for span exporters."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List

from app.core.observability.exporter import (
    LoguruExporter,
    MultiExporter,
    NullExporter,
    SampledExporter,
)
from app.core.observability.span import RequestSpan


def _span(status: str = "ok") -> RequestSpan:
    span = RequestSpan.start(request_id="r", method="POST", path="/v1/messages")
    span.status = status  # type: ignore[assignment]
    return span


@dataclass
class _Recorder:
    seen: List[RequestSpan] = field(default_factory=list)

    def export(self, span: RequestSpan) -> None:
        self.seen.append(span)


@dataclass
class _Raiser:
    def export(self, span: RequestSpan) -> None:
        raise RuntimeError("boom")


class TestNullExporter:
    def test_drops_silently(self):
        NullExporter().export(_span())


class TestLoguruExporter:
    def test_emits_structured_record(self):
        from loguru import logger

        records = []
        logger.remove()
        try:
            logger.add(lambda m: records.append(m.record), level="INFO")
            LoguruExporter().export(_span())
        finally:
            logger.remove()
        assert len(records) == 1
        extra = records[0]["extra"]
        assert extra["event"] == "request.complete"
        assert extra["request_id"] == "r"


class TestMultiExporter:
    def test_fans_out(self):
        a, b = _Recorder(), _Recorder()
        MultiExporter([a, b]).export(_span())
        assert len(a.seen) == 1 and len(b.seen) == 1

    def test_one_failure_does_not_stop_others(self):
        a, b = _Raiser(), _Recorder()
        MultiExporter([a, b]).export(_span())
        assert len(b.seen) == 1


class TestSampledExporter:
    def test_full_rate_always_exports(self):
        inner = _Recorder()
        sampled = SampledExporter(inner, ok_rate=1.0, error_rate=1.0)
        for _ in range(100):
            sampled.export(_span("ok"))
        assert len(inner.seen) == 100

    def test_zero_rate_drops_all(self):
        inner = _Recorder()
        sampled = SampledExporter(inner, ok_rate=0.0, error_rate=0.0)
        for _ in range(100):
            sampled.export(_span("ok"))
        assert inner.seen == []

    def test_errors_pinned_to_one(self):
        """ok sampled out, errors still kept when error_rate=1.0."""
        inner = _Recorder()
        sampled = SampledExporter(inner, ok_rate=0.0, error_rate=1.0)
        for _ in range(50):
            sampled.export(_span("ok"))
            sampled.export(_span("exception"))
        # Exactly 50 error spans, zero ok spans
        assert len(inner.seen) == 50
        assert all(s.status == "exception" for s in inner.seen)

    def test_partial_rate_approximates(self):
        random.seed(1234)
        inner = _Recorder()
        sampled = SampledExporter(inner, ok_rate=0.5, error_rate=1.0)
        for _ in range(1000):
            sampled.export(_span("ok"))
        # Within a reasonable band given the fixed seed
        assert 400 <= len(inner.seen) <= 600
