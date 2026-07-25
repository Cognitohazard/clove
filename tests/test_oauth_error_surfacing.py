"""Upstream OAuth failures must survive the trip through clove's error handling.

Both a stale-session 403 and a state-less token grant used to reach the operator as
generic messages, because the app parsed the upstream body and then dropped it.
"""

import pytest

from app.core.account import Account, AuthType
from app.core.exceptions import AppError
from app.services.oauth import (
    SESSION_STALE_ERROR_CODE,
    OAuthAuthenticator,
    _upstream_detail,
)


class _FakeResponse:
    """Minimal stand-in for the http_client Response protocol."""

    def __init__(self, payload, status_code=400):
        self._payload = payload
        self.status_code = status_code

    async def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _cookie_only() -> Account:
    return Account(
        organization_uuid="eb613092-caf1-4d59-8ad6-f29a32539983",
        cookie_value="sessionKey=whatever",
        auth_type=AuthType.COOKIE_ONLY,
    )


async def test_upstream_detail_reads_both_error_shapes():
    anthropic = await _upstream_detail(
        _FakeResponse(
            {
                "type": "error",
                "error": {
                    "type": "permission_error",
                    "message": "Session is not fresh enough to grant elevated access.",
                    "details": {"error_code": SESSION_STALE_ERROR_CODE},
                },
            }
        )
    )
    assert "Session is not fresh enough" in anthropic

    plain_oauth = await _upstream_detail(
        _FakeResponse(
            {
                "error": "invalid_grant",
                "error_description": "Refresh token not found or invalid",
            }
        )
    )
    assert "Refresh token not found" in plain_oauth

    # Unparseable body must not explode — callers supply their own fallback.
    assert await _upstream_detail(_FakeResponse(None)) == ""


async def test_stale_session_blocks_upgrade_until_cookie_is_replaced(monkeypatch):
    """A stale-session 403 is permanent for this cookie: stop retrying it."""
    account = _cookie_only()
    monkeypatch.setattr(Account, "save", lambda self: None)
    assert account.needs_oauth_upgrade is True

    auth = OAuthAuthenticator()

    async def _boom(*_a, **_kw):
        raise AppError(
            error_code=400124,
            message_key="claudeClient.authenticationError",
            status_code=400,
            context={"upstream_error_code": SESSION_STALE_ERROR_CODE},
        )

    monkeypatch.setattr(auth, "get_organization_info", _boom)

    assert await auth.authenticate_account(account) is False
    assert account.oauth_upgrade_blocked_reason == SESSION_STALE_ERROR_CODE
    # The periodic self-heal loop keys off this property — it must now say "don't".
    assert account.needs_oauth_upgrade is False

    # Replacing the cookie clears the verdict; the setter owns this, not the callers.
    account.cookie_value = "sessionKey=freshly-signed-in"
    assert account.oauth_upgrade_blocked_reason is None
    assert account.needs_oauth_upgrade is True


async def test_transient_failure_does_not_block_upgrade(monkeypatch):
    """Only the stale-session verdict is permanent; ordinary errors stay retryable."""
    account = _cookie_only()
    monkeypatch.setattr(Account, "save", lambda self: None)
    auth = OAuthAuthenticator()

    async def _boom(*_a, **_kw):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(auth, "get_organization_info", _boom)

    assert await auth.authenticate_account(account) is False
    assert account.oauth_upgrade_blocked_reason is None
    assert account.needs_oauth_upgrade is True


@pytest.mark.parametrize(
    "code, explicit_state, expected",
    [
        ("thecode#thestate", "tracked", "thestate"),  # the code's tail wins
        ("thecode", "tracked", "tracked"),  # caller-tracked state
        ("thecode", None, "theverifier"),  # legacy: UI reused the verifier
    ],
)
async def test_exchange_always_sends_state(monkeypatch, code, explicit_state, expected):
    """Upstream rejects an authorization_code grant with no state, so always send one."""
    auth = OAuthAuthenticator()
    sent = {}

    async def _capture(url, data, **_kw):
        sent.update(data)
        return _FakeResponse(
            {"access_token": "a", "refresh_token": "r", "expires_in": 1},
            status_code=200,
        )

    monkeypatch.setattr(auth, "_token_request", _capture)

    await auth.exchange_token(code, "theverifier", explicit_state)

    assert sent["state"] == expected
    assert sent["code"] == "thecode"
    assert sent["code_verifier"] == "theverifier"
