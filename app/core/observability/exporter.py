"""Span exporters — where a finished ``RequestSpan`` goes.

The protocol lets us compose behaviors (``SampledExporter`` wraps any
exporter; ``MultiExporter`` fans out) and swap targets (Loguru today,
OpenTelemetry tomorrow) without touching the middleware that produces
the span.
"""

from __future__ import annotations

import random
from typing import Iterable, Protocol, runtime_checkable

from loguru import logger

from app.core.observability.span import RequestSpan


@runtime_checkable
class SpanExporter(Protocol):
    def export(self, span: RequestSpan) -> None: ...


class NullExporter:
    """Drops every span. Used when observability is disabled."""

    def export(self, span: RequestSpan) -> None:
        return


class LoguruExporter:
    """Emit one structured record per span via loguru.

    The record is tagged ``event=request.complete`` so a dedicated sink
    can filter for it while the same record also flows through the
    human-readable stdout sink.
    """

    def export(self, span: RequestSpan) -> None:
        logger.bind(**span.to_record()).info("request.complete")


class MultiExporter:
    """Fan out a span to several exporters.

    A failure in one exporter does not prevent the others from running;
    observability must never crash a request.
    """

    def __init__(self, exporters: Iterable[SpanExporter]):
        self._exporters = list(exporters)

    def export(self, span: RequestSpan) -> None:
        for exporter in self._exporters:
            try:
                exporter.export(span)
            except Exception as exc:
                logger.warning(
                    f"Span exporter {type(exporter).__name__} raised: {exc!r}"
                )


class SampledExporter:
    """Rate-limit OK traffic; always keep errors.

    Set ``ok_rate`` below 1.0 for high-volume deployments; ``error_rate``
    stays at 1.0 by default so you never miss a failure.
    """

    def __init__(
        self,
        inner: SpanExporter,
        *,
        ok_rate: float = 1.0,
        error_rate: float = 1.0,
    ):
        self._inner = inner
        self._ok_rate = ok_rate
        self._error_rate = error_rate

    def export(self, span: RequestSpan) -> None:
        rate = self._ok_rate if span.status == "ok" else self._error_rate
        if rate >= 1.0 or random.random() < rate:
            self._inner.export(span)
