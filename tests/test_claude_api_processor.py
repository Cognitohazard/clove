"""The OAuth processor sends every request as the Claude Code CLI would (no network).

A fake account and a stub session stand in for the account pool and the rnet
transport; the assertions are on what would go over the wire.
"""

import json
import math

import pytest
from fastapi import Request
from rnet.exceptions import ConnectionResetError as RnetConnectionResetError
from rnet.exceptions import TimeoutError as RnetTimeoutError

import app.processors.claude_ai.claude_api_processor as processor_module
import app.utils.claude_code_identity as identity
from app.core.account import Account, AuthType, OAuthToken
from app.core.exceptions import (
    ClaudeHttpError,
    MalformedRequestBodyError,
    ProxyConnectionError,
)
from app.core.http_client import OAuthSession
from app.processors.claude_ai import ClaudeAIContext
from app.processors.claude_ai.claude_api_processor import ClaudeAPIProcessor
from app.utils.claude_code_identity import HEADER_ORDER, IDENTITY_TEXT, verify_billing
from app.views.messages_view import MessagesRequestView
from tests.test_oauth_transport import _FakeRnetClient, _StubResponse, _StubSession

_BODY = {
    "model": "claude-opus-5-5",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "Say hi"}],
}
_PROMPT_ID = "0b6f3c1e-7a2d-4c8e-9f10-2a3b4c5d6e7f"
_PROXY = "http://proxy.test:8080"


@pytest.fixture
def account():
    return Account(
        organization_uuid="org-uuid-0000",
        oauth_token=OAuthToken("sk-ant-oat01-x", "r", 0.0, account_uuid="acc-uuid"),
        auth_type=AuthType.OAUTH_ONLY,
    )


@pytest.fixture
def wire(monkeypatch, account):
    """Route the processor to ``account`` and return a function installing a stub session."""
    monkeypatch.setattr(identity, "_version", "2.1.283")
    monkeypatch.setattr(identity, "_pinned", False)
    monkeypatch.setattr(Account, "save", lambda self: None)  # never touch accounts.json
    monkeypatch.setattr(
        processor_module.cache_service, "process_messages", lambda *_a: (None, [])
    )

    async def get_account_for_oauth(**_kw):
        return account

    async def get_proxy(**_kw):
        return None

    monkeypatch.setattr(
        processor_module.account_manager, "get_account_for_oauth", get_account_for_oauth
    )
    monkeypatch.setattr(processor_module.proxy_service, "get_proxy", get_proxy)

    def install(*responses) -> _StubSession:
        session = _StubSession(*responses)
        monkeypatch.setattr(
            processor_module, "create_oauth_session", lambda **_kw: session
        )
        return session

    return install


@pytest.fixture
def proxy(monkeypatch):
    """Route through ``_PROXY``; return the proxies marked unhealthy."""
    marked = []

    async def get_proxy(**_kw):
        return _PROXY

    async def mark_unhealthy(proxy_url, reason="unknown"):
        marked.append(proxy_url)

    service = processor_module.proxy_service
    monkeypatch.setattr(service, "get_proxy", get_proxy)
    monkeypatch.setattr(service, "mark_unhealthy", mark_unhealthy)
    return marked


def _ok(request_id="req_011CAbc", payload=None):
    return _StubResponse(
        200,
        payload or {"type": "message", "content": []},
        headers={"content-type": "application/json", "request-id": request_id},
    )


def _context(body=None, headers=None, raw=None) -> ClaudeAIContext:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/messages",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
        }
    )
    raw = raw if raw is not None else json.dumps(body or _BODY).encode()
    return ClaudeAIContext(original_request=request, view=MessagesRequestView(raw))


def _billing(data: bytes) -> str:
    return json.loads(data)["system"][0]["text"]


async def test_request_goes_out_as_the_cli(wire):
    session = wire(_ok())
    context = await ClaudeAPIProcessor().process(_context())

    assert context.response.status_code == 200
    chunks = [chunk async for chunk in context.response.body_iterator]
    assert json.loads(b"".join(chunks)) == {"type": "message", "content": []}

    call = session.calls[0]
    assert call.method == "POST"
    assert call.url.endswith("/v1/messages?beta=true")
    assert [name for name, _ in call.headers] == [
        n for n in HEADER_ORDER if n not in {"Host", "Content-Length"}
    ]
    assert verify_billing(call.data) == (True, True)

    headers = dict(call.headers)
    body = json.loads(call.data)
    user_id = json.loads(body["metadata"]["user_id"])
    assert headers["X-Claude-Code-Session-Id"] == user_id["session_id"]
    assert user_id["account_uuid"] == "acc-uuid"
    assert headers["x-claude-code-request-class"] == "main"
    assert headers["User-Agent"] == "claude-cli/2.1.283 (external, cli)"
    assert "cc_prev_req" not in _billing(call.data)


async def test_next_main_turn_carries_the_previous_request_id(wire):
    session = wire(_ok("req_011First"), _ok("req_011Second"))
    processor = ClaudeAPIProcessor()
    await processor.process(_context())
    turn2 = {
        **_BODY,
        "messages": _BODY["messages"]
        + [
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "Bye"},
        ],
    }
    await processor.process(_context(turn2))

    first, second = session.calls
    assert (
        dict(first.headers)["X-Claude-Code-Session-Id"]
        == (dict(second.headers)["X-Claude-Code-Session-Id"])
    )
    assert " cc_prev_req=req_011First;" in _billing(second.data)
    assert verify_billing(second.data) == (True, True)


async def test_cc_prev_req_follows_the_adopted_client_session(wire):
    """Two client sessions with the same first message keep separate chains, and a
    session keeps its chain when its first message changes (compaction)."""

    def turn(session_id, messages=_BODY["messages"]):
        user_id = json.dumps({"session_id": session_id})
        return {**_BODY, "messages": messages, "metadata": {"user_id": user_id}}

    a = "11111111-1111-4111-8111-111111111111"
    b = "22222222-2222-4222-8222-222222222222"
    session = wire(*(_ok(f"req_011{n}") for n in ("A1", "B1", "A2", "A3")))
    processor = ClaudeAPIProcessor()
    later = _BODY["messages"] + [
        {"role": "assistant", "content": "Hi."},
        {"role": "user", "content": "Bye"},
    ]
    compacted = [{"role": "user", "content": "Summary of the chat so far"}]
    for body in (turn(a), turn(b), turn(a, later), turn(a, compacted)):
        await processor.process(_context(body))

    a1, b1, a2, a3 = session.calls
    assert dict(b1.headers)["X-Claude-Code-Session-Id"] == b
    assert "cc_prev_req" not in _billing(b1.data)
    assert " cc_prev_req=req_011A1;" in _billing(a2.data)
    assert dict(a3.headers)["X-Claude-Code-Session-Id"] == a
    assert " cc_prev_req=req_011A2;" in _billing(a3.data)


async def test_auxiliary_requests_neither_send_nor_record_cc_prev_req(wire):
    session = wire(_ok("req_011First"), _ok("req_011Aux"), _ok("req_011Third"))
    processor = ClaudeAPIProcessor()
    await processor.process(_context())
    aux = {"x-claude-code-request-class": "auxiliary"}
    await processor.process(_context(headers=aux))
    await processor.process(_context())

    first, aux_call, third = session.calls
    assert dict(aux_call.headers)["x-claude-code-request-class"] == "auxiliary"
    assert "cc_prev_req" not in _billing(aux_call.data)
    assert " cc_prev_req=req_011First;" in _billing(third.data)


async def test_version_gate_bumps_and_retries_exactly_once(wire):
    def gate(version):
        message = (
            f"Claude Code 2.1.283 does not support this model; "
            f"version {version} or newer is required"
        )
        payload = {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        }
        return _StubResponse(400, payload)

    session = wire(gate("9.9.9"), _ok())
    context = await ClaudeAPIProcessor().process(_context())
    assert context.response.status_code == 200
    assert len(session.calls) == 2
    retry = session.calls[1]
    assert dict(retry.headers)["User-Agent"] == "claude-cli/9.9.9 (external, cli)"
    assert "cc_version=9.9.9." in _billing(retry.data)
    assert verify_billing(retry.data) == (True, True)

    # A second gate on the retry is surfaced, not retried again, but still bumps.
    session = wire(gate("9.9.10"), gate("9.9.11"))
    with pytest.raises(ClaudeHttpError):
        await ClaudeAPIProcessor().process(_context())
    assert len(session.calls) == 2
    assert identity.cli_version() == "9.9.11"


async def test_a_gate_below_the_current_version_is_not_retried(wire):
    payload = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "Claude Code 2.1.283 does not support this model; "
            "version 1.0.0 or newer is required",
        },
    }
    session = wire(_StubResponse(400, payload))
    with pytest.raises(ClaudeHttpError):
        await ClaudeAPIProcessor().process(_context())
    assert len(session.calls) == 1


async def test_an_unserializable_body_is_a_malformed_request(wire):
    session = wire(_ok())
    raw = json.dumps({**_BODY, "x_extra": math.nan}).encode()  # json.loads accepts NaN
    with pytest.raises(MalformedRequestBodyError):
        await ClaudeAPIProcessor().process(_context(raw=raw))
    assert session.calls == []


@pytest.mark.parametrize("inject", [True, False])
async def test_identity_block_follows_the_setting(wire, monkeypatch, inject):
    monkeypatch.setattr(
        processor_module.settings, "inject_claude_code_system_prompt", inject
    )
    session = wire(_ok())
    await ClaudeAPIProcessor().process(_context())

    texts = [block["text"] for block in json.loads(session.calls[0].data)["system"]]
    assert texts[0].startswith("x-anthropic-billing-header: ")
    assert texts[1:] == ([IDENTITY_TEXT] if inject else [])


async def test_a_claude_code_clients_prompt_id_and_request_class_are_adopted(wire):
    session = wire(_ok())
    inbound = {
        "x-claude-code-prompt-id": _PROMPT_ID,
        "x-claude-code-request-class": "auxiliary",
    }
    await ClaudeAPIProcessor().process(_context(headers=inbound))

    call = session.calls[0]
    headers = dict(call.headers)
    assert headers["x-claude-code-prompt-id"] == _PROMPT_ID
    assert headers["x-claude-code-request-class"] == "auxiliary"
    assert f"cc_prompt_id={_PROMPT_ID};" in _billing(call.data)


async def test_a_malformed_request_class_falls_back_to_main(wire):
    session = wire(_ok())
    inbound = {"x-claude-code-request-class": "Main Request!"}
    await ClaudeAPIProcessor().process(_context(headers=inbound))
    assert dict(session.calls[0].headers)["x-claude-code-request-class"] == "main"


async def test_a_400_without_a_string_message_is_still_an_http_error(wire):
    payload = {"type": "error", "error": {"type": "api_error", "message": None}}
    session = wire(_StubResponse(400, payload))
    with pytest.raises(ClaudeHttpError):
        await ClaudeAPIProcessor().process(_context())
    assert len(session.calls) == 1


def _transport(monkeypatch, client):
    monkeypatch.setattr(processor_module.settings, "request_retry_interval", 0)
    monkeypatch.setattr(
        processor_module, "create_oauth_session", lambda **_kw: OAuthSession(client)
    )


async def test_a_reset_after_the_retry_marks_the_proxy(wire, proxy, monkeypatch):
    client = _FakeRnetClient(
        RnetConnectionResetError("reset"), RnetConnectionResetError("reset again")
    )
    _transport(monkeypatch, client)
    with pytest.raises(ProxyConnectionError):
        await ClaudeAPIProcessor().process(_context())
    assert len(client.calls) == 2
    assert proxy == [_PROXY]


async def test_a_timeout_does_not_mark_the_proxy(wire, proxy, monkeypatch):
    client = _FakeRnetClient(RnetTimeoutError("slow generation"))
    _transport(monkeypatch, client)
    with pytest.raises(RnetTimeoutError):
        await ClaudeAPIProcessor().process(_context())
    assert proxy == []
