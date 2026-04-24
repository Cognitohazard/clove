"""ContextVar-backed access to the current request span.

Any processor, service, or log statement can read the active span via
``current_span()``. Writers are the middleware (sets it) and the pipeline
processors (mutate its attributes). ContextVars propagate into
``asyncio.Task``s so sub-tasks see the same span without plumbing.
"""

from __future__ import annotations

import contextvars
from typing import Optional

from app.core.observability.span import RequestSpan


_current_span: contextvars.ContextVar[Optional[RequestSpan]] = contextvars.ContextVar(
    "clove_current_span", default=None
)


def current_span() -> Optional[RequestSpan]:
    """Return the span for the in-flight request, or ``None`` outside one."""
    return _current_span.get()


def _set_span(span: RequestSpan) -> contextvars.Token:
    return _current_span.set(span)


def _reset_span(token: contextvars.Token) -> None:
    _current_span.reset(token)
