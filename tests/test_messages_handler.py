"""MessagesHandler phase routing.

The handler replaces the flag-driven pipeline with three explicit phases.
These tests pin the routing rules — what runs when, what gets skipped.
"""

from typing import List
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from app.handlers.messages_handler import MessagesHandler
from app.processors.base import BaseProcessor
from app.processors.claude_ai import ClaudeAIContext
from app.views.messages_view import MessagesRequestView


class RecordingProcessor(BaseProcessor):
    """Test double that records when it ran and optionally mutates context."""

    def __init__(self, name: str, on_process=None):
        self._name = name
        self._on_process = on_process
        self.call_log: List[str] = []

    async def process(self, context: ClaudeAIContext) -> ClaudeAIContext:
        self.call_log.append(self._name)
        if self._on_process is not None:
            self._on_process(context)
        return context

    @property
    def name(self) -> str:
        return self._name


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/messages",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def _context_with_view() -> ClaudeAIContext:
    body = b'{"model":"claude-opus-4-7","max_tokens":1,"messages":[{"role":"user","content":"x"}]}'
    view = MessagesRequestView(body)
    return ClaudeAIContext(original_request=_request(), view=view)


def _make_handler(
    test_message=None, tool_result=None, oauth=None, web=None, post=None
):
    return MessagesHandler(
        test_message=test_message or RecordingProcessor("test"),
        tool_result=tool_result or RecordingProcessor("tool"),
        oauth=oauth or RecordingProcessor("oauth"),
        web=web or RecordingProcessor("web"),
        post_processors=post or [],
    )


@pytest.mark.asyncio
async def test_tavern_test_short_circuits_everything():
    """When tavern test sets a response, no strategy and no post-chain runs."""

    def set_response(ctx):
        ctx.response = JSONResponse({"ok": 1})

    test_proc = RecordingProcessor("test", on_process=set_response)
    oauth = RecordingProcessor("oauth")
    web = RecordingProcessor("web")
    post = RecordingProcessor("post")

    handler = _make_handler(
        test_message=test_proc, oauth=oauth, web=web, post=[post]
    )
    ctx = await handler.handle(_context_with_view())

    assert test_proc.call_log == ["test"]
    assert oauth.call_log == []
    assert web.call_log == []
    assert post.call_log == []
    assert ctx.response is not None


@pytest.mark.asyncio
async def test_oauth_success_skips_web_and_post():
    """OAuth setting a Response is the success path; Web and post-chain skip."""

    def set_response(ctx):
        ctx.response = JSONResponse({"ok": 1})

    oauth = RecordingProcessor("oauth", on_process=set_response)
    web = RecordingProcessor("web")
    post = RecordingProcessor("post")

    handler = _make_handler(oauth=oauth, web=web, post=[post])
    await handler.handle(_context_with_view())

    assert oauth.call_log == ["oauth"]
    assert web.call_log == []
    assert post.call_log == []


@pytest.mark.asyncio
async def test_oauth_unavailable_falls_through_to_web_then_post():
    """When OAuth doesn't produce a response, Web runs and post-chain follows."""
    oauth = RecordingProcessor("oauth")  # leaves response None
    web = RecordingProcessor("web")
    post1 = RecordingProcessor("post1")
    post2 = RecordingProcessor("post2")

    handler = _make_handler(oauth=oauth, web=web, post=[post1, post2])
    await handler.handle(_context_with_view())

    assert oauth.call_log == ["oauth"]
    assert web.call_log == ["web"]
    assert post1.call_log == ["post1"]
    assert post2.call_log == ["post2"]


@pytest.mark.asyncio
async def test_tool_result_resumed_stream_skips_strategy_runs_post():
    """Tool-result resumption seeds original_stream; OAuth and Web are skipped."""

    async def fake_stream():
        yield "x"

    def seed_stream(ctx):
        ctx.original_stream = fake_stream()

    tool = RecordingProcessor("tool", on_process=seed_stream)
    oauth = RecordingProcessor("oauth")
    web = RecordingProcessor("web")
    post = RecordingProcessor("post")

    handler = _make_handler(tool_result=tool, oauth=oauth, web=web, post=[post])
    await handler.handle(_context_with_view())

    assert tool.call_log == ["tool"]
    assert oauth.call_log == []
    assert web.call_log == []
    assert post.call_log == ["post"]


@pytest.mark.asyncio
async def test_session_cleanup_on_exception():
    """If a phase raises, claude_session is removed from the session manager."""

    def boom(_ctx):
        raise RuntimeError("kaboom")

    oauth = RecordingProcessor("oauth", on_process=boom)
    handler = _make_handler(oauth=oauth)

    ctx = _context_with_view()
    fake_session = MagicMock()
    fake_session.session_id = "test-session"
    ctx.claude_session = fake_session

    # Patch the session_manager.remove_session call
    import app.handlers.messages_handler as mod

    original = mod.session_manager.remove_session
    mod.session_manager.remove_session = AsyncMock()
    try:
        with pytest.raises(RuntimeError):
            await handler.handle(ctx)
        mod.session_manager.remove_session.assert_called_once_with("test-session")
    finally:
        mod.session_manager.remove_session = original
