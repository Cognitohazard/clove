"""OAuth transport and the side requests that use it (no network).

``OAuthSession`` must hand rnet the caller's headers verbatim plus an
``orig_headers`` list that places the two headers rnet computes (``Host``,
``Content-Length``) where the CLI sends them. The quota probe and both
``/v1/models`` callers must go out with CLI headers too.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import Request
from rnet.exceptions import ConnectionError as RnetConnectionError
from rnet.exceptions import ConnectionResetError as RnetConnectionResetError
from rnet.exceptions import RequestError as RnetRequestError

import app.api.routes.models as models_route
import app.core.http_client as http_client
import app.services.oauth as oauth_service
from app.core.account import Account, AccountStatus, AuthType, OAuthToken
from app.core.http_client import AsyncSession, OAuthSession, _wire_header_order
from app.services.account import AccountManager, _QUOTA_PROBE_BETAS
from app.utils.claude_code_identity import HEADER_ORDER, build_api_headers

_COMPUTED = {"Host", "Content-Length"}


def test_orig_headers_put_host_after_connection_and_length_last_for_a_body():
    names = [n for n in HEADER_ORDER if n not in _COMPUTED]
    assert _wire_header_order(names, has_body=True) == HEADER_ORDER


def test_orig_headers_for_a_get_have_no_content_length():
    names = ["Accept", "Authorization", "Connection", "Accept-Encoding"]
    assert _wire_header_order(names, has_body=False) == [
        "Accept",
        "Authorization",
        "Connection",
        "Host",
        "Accept-Encoding",
    ]


def test_orig_headers_append_host_without_connection():
    assert _wire_header_order(["Accept"], has_body=False) == ["Accept", "Host"]


class _FakeRnetResponse:
    def __init__(self, status=200):
        self.status = SimpleNamespace(as_int=lambda: status)
        self.headers = [(b"request-id", b"req_1")]


class _FakeRnetClient:
    def __init__(self, *failures):
        self.calls = []
        self._failures = list(failures)

    async def request(self, method, url, **kwargs):
        self.calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        if self._failures:
            raise self._failures.pop(0)
        return _FakeRnetResponse()


async def test_session_sends_headers_verbatim_with_the_wire_order():
    client = _FakeRnetClient()
    headers = [("Accept", "application/json"), ("Connection", "keep-alive")]
    response = await OAuthSession(client, timeout=7).request(
        "POST", "https://api.example/v1/messages?beta=true", headers=headers, data=b"{}"
    )

    call = client.calls[0]
    assert call.method == http_client.RnetMethod.POST
    assert call.orig_headers == ["Accept", "Connection", "Host", "Content-Length"]
    assert call.default_headers is False
    assert call.timeout == 7
    assert call.body == b"{}"
    assert call.headers["Accept"] == b"application/json"
    assert not hasattr(call, "query")
    assert response.status_code == 200
    assert response.headers["request-id"] == "req_1"


async def test_session_get_passes_params_and_a_timeout_override():
    client = _FakeRnetClient()
    await OAuthSession(client, timeout=7).request(
        "GET",
        "https://api.example/v1/models",
        headers={"Accept": "application/json"},
        params={"limit": "1000"},
        timeout=3,
    )
    call = client.calls[0]
    assert call.query == {"limit": "1000"}
    assert call.timeout == 3
    assert not hasattr(call, "body")
    assert call.orig_headers == ["Accept", "Host"]


async def test_a_dead_pooled_connection_is_retried_once(monkeypatch):
    monkeypatch.setattr(http_client.settings, "request_retry_interval", 0)
    client = _FakeRnetClient(RnetConnectionResetError("reset"))
    response = await OAuthSession(client, timeout=7).request(
        "POST", "https://api.example/", headers={}, data=b"x"
    )
    assert response.status_code == 200
    assert len(client.calls) == 2

    twice = _FakeRnetClient(
        RnetConnectionResetError("a"), RnetConnectionResetError("b")
    )
    with pytest.raises(RnetConnectionResetError):
        await OAuthSession(twice, timeout=7).request(
            "POST", "https://api.example/", headers={}, data=b"x"
        )
    assert len(twice.calls) == 2


async def test_the_retried_attempt_counts_itself_like_the_stainless_sdk(monkeypatch):
    monkeypatch.setattr(http_client.settings, "request_retry_interval", 0)
    client = _FakeRnetClient(RnetRequestError("connection closed"))
    headers = [("Accept", "application/json"), ("X-Stainless-Retry-Count", "0")]
    await OAuthSession(client, timeout=7).request(
        "POST", "https://api.example/", headers=headers, data=b"x"
    )

    first, retry = client.calls
    assert first.headers["X-Stainless-Retry-Count"] == b"0"
    assert retry.headers["X-Stainless-Retry-Count"] == b"1"
    assert retry.headers["Accept"] == b"application/json"
    assert retry.orig_headers == first.orig_headers


async def test_a_failed_connect_is_not_retried():
    client = _FakeRnetClient(RnetConnectionError("refused"))
    with pytest.raises(RnetConnectionError):
        await OAuthSession(client, timeout=7).request(
            "POST", "https://api.example/", headers={}, data=b"x"
        )
    assert len(client.calls) == 1


async def test_request_takes_headers_and_body_by_keyword_only():
    with pytest.raises(TypeError):
        await OAuthSession(_FakeRnetClient()).request(
            "POST", "https://api.example/", [("Accept", "a")], b"x"
        )


async def test_the_default_timeout_is_read_at_call_time(monkeypatch):
    client = _FakeRnetClient()
    session = OAuthSession(client)
    monkeypatch.setattr(http_client.settings, "request_timeout", 11)
    await session.request("GET", "https://api.example/", headers={})
    assert client.calls[0].timeout == 11


def test_one_client_per_proxy():
    first = http_client.create_oauth_session(proxy="http://127.0.0.1:9")
    again = http_client.create_oauth_session(proxy="http://127.0.0.1:9")
    other = http_client.create_oauth_session(proxy=None)
    assert first._client is again._client
    assert other._client is not first._client


# --------------------------------------------------------------------------
# Callers
# --------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload

    async def json(self):
        return self._payload

    async def aiter_bytes(self, chunk_size=None):
        yield json.dumps(self._payload).encode()


class _StubSession(AsyncSession):
    """Records each request and answers with ``responses`` in turn (the last repeats)."""

    def __init__(self, *responses):
        self.calls = []
        self._responses = list(responses)

    async def request(self, method, url, headers, data=None, stream=False, **kwargs):
        pairs = list(headers.items() if isinstance(headers, dict) else headers)
        self.calls.append(
            SimpleNamespace(method=method, url=url, headers=pairs, data=data, **kwargs)
        )
        return (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )

    async def close(self):
        pass


def _oauth_account() -> Account:
    account = Account(
        organization_uuid="org-uuid-0000",
        oauth_token=OAuthToken("sk-ant-oat01-x", "r", 0.0, account_uuid="acc"),
        auth_type=AuthType.OAUTH_ONLY,
    )
    account.status = AccountStatus.RATE_LIMITED
    return account


def _no_proxy(monkeypatch):
    async def get_proxy(**_kwargs):
        return None

    monkeypatch.setattr(oauth_service.proxy_service, "get_proxy", get_proxy)


async def test_quota_probe_has_the_cli_probe_shape(monkeypatch):
    _no_proxy(monkeypatch)
    session = _StubSession(_StubResponse(200))
    monkeypatch.setattr(http_client, "create_oauth_session", lambda **_kw: session)
    account = _oauth_account()

    assert await AccountManager._probe_rate_limit(None, account) == ("valid", None)

    call = session.calls[0]
    assert call.url.endswith("/v1/messages?beta=true")
    names = [name for name, _ in call.headers]
    assert names == [
        n for n in HEADER_ORDER if n not in _COMPUTED | {"x-claude-code-prompt-id"}
    ]
    headers = dict(call.headers)
    assert headers["x-claude-code-request-class"] == "auxiliary"
    assert headers["anthropic-beta"] == _QUOTA_PROBE_BETAS
    assert headers["Authorization"] == "Bearer sk-ant-oat01-x"

    body = json.loads(call.data)
    assert call.data == json.dumps(body, separators=(",", ":")).encode()
    assert list(body) == ["model", "max_tokens", "messages", "metadata"]
    assert body["max_tokens"] == 1 and "system" not in body
    user_id = json.loads(body["metadata"]["user_id"])
    assert list(user_id) == ["device_id", "account_uuid", "session_id"]
    assert user_id["device_id"] == account.device_id
    assert user_id["account_uuid"] == "acc"
    assert user_id["session_id"] == headers["X-Claude-Code-Session-Id"]


async def test_quota_probe_reads_the_429_reset(monkeypatch):
    _no_proxy(monkeypatch)
    reset = {"anthropic-ratelimit-unified-reset": "1790485200"}  # epoch seconds
    session = _StubSession(_StubResponse(429, headers=reset))
    monkeypatch.setattr(http_client, "create_oauth_session", lambda **_kw: session)

    status, resets_at = await AccountManager._probe_rate_limit(None, _oauth_account())
    assert status == "rate_limited"
    assert resets_at.isoformat() == "2026-09-27T05:00:00+00:00"


def _assert_cli_api_headers(pairs, *, betas=None):
    expected = build_api_headers(
        "sk-ant-oat01-x", client_beta_header=betas, cli_version="0.0.0"
    )
    assert [name for name, _ in pairs] == [name for name, _ in expected]
    headers = dict(pairs)
    assert headers["anthropic-version"] == "2023-06-01"
    assert "anthropic-request-id" not in headers
    return headers


async def test_model_discovery_uses_cli_api_headers(monkeypatch):
    _no_proxy(monkeypatch)
    session = _StubSession(_StubResponse(200, {"data": [{"id": "claude-x"}]}))
    monkeypatch.setattr(oauth_service, "create_oauth_session", lambda **_kw: session)

    models = await oauth_service.oauth_authenticator.fetch_available_models(
        _oauth_account()
    )
    assert models == ["claude-x"]
    call = session.calls[0]
    assert call.params == {"limit": "1000"}
    _assert_cli_api_headers(call.headers)


async def test_models_route_drops_client_version_and_request_id(monkeypatch):
    session = _StubSession(_StubResponse(200, {"data": []}))
    monkeypatch.setattr(models_route, "create_oauth_session", lambda **_kw: session)

    async def get_account(**_kw):
        return _oauth_account()

    async def get_proxy(**_kw):
        return None

    monkeypatch.setattr(
        models_route.account_manager, "get_account_for_oauth", get_account
    )
    monkeypatch.setattr(models_route.proxy_service, "get_proxy", get_proxy)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/models",
            "query_string": b"",
            "headers": [
                (b"anthropic-version", b"2099-01-01"),
                (b"anthropic-request-id", b"req_client"),
                (b"anthropic-beta", b"some-beta-2026-01-01"),
            ],
        }
    )

    await models_route._proxy_models_request(request, "/v1/models", {"limit": "20"})

    call = session.calls[0]
    assert call.params == {"limit": "20"}
    headers = _assert_cli_api_headers(call.headers, betas="some-beta-2026-01-01")
    assert headers["anthropic-beta"].endswith(",some-beta-2026-01-01")
