"""Account-side support for model-aware OAuth selection.

Covers the data plumbing and routing that let
``AccountManager.get_account_for_oauth`` serve a model-gated request only from
accounts whose discovered ``/v1/models`` list confirms support — an undiscovered
account is not routed model-gated traffic (there is no static model list to
guess from).
"""

import asyncio

import pytest

import app.services.account as account_service
from app.core.account import Account, AuthType, AccountStatus, OAuthToken
from app.core.exceptions import NoAccountsAvailableError
from app.services.account import AccountManager


def _oauth_account(
    org_uuid: str,
    *,
    available_models=None,
    capabilities=None,
) -> Account:
    return Account(
        organization_uuid=org_uuid,
        oauth_token=OAuthToken("a", "r", 0.0),
        auth_type=AuthType.OAUTH_ONLY,
        capabilities=capabilities or ["chat"],
        available_models=available_models,
    )


def _manager(*accounts: Account) -> AccountManager:
    """A manager with injected accounts, skipping __init__'s file loading."""
    mgr = AccountManager.__new__(AccountManager)
    mgr._accounts = {a.organization_uuid: a for a in accounts}
    return mgr


def test_can_serve_model_unknown_returns_none():
    """No discovery yet -> account must not be routed model-gated traffic."""
    account = _oauth_account("uuid1", available_models=None)
    assert account.can_serve_model("claude-opus-4-7") is None


def test_can_serve_model_authoritative_yes():
    account = _oauth_account(
        "uuid1", available_models=["claude-opus-4-7", "claude-sonnet-4-6"]
    )
    assert account.can_serve_model("claude-opus-4-7") is True


def test_can_serve_model_authoritative_no():
    account = _oauth_account(
        "uuid1", available_models=["claude-haiku-4-5"]
    )
    assert account.can_serve_model("claude-opus-4-7") is False


def test_account_round_trips_available_models_through_dict():
    account = _oauth_account(
        "uuid1", available_models=["claude-opus-4-7", "claude-sonnet-4-6"]
    )
    account.status = AccountStatus.VALID
    blob = account.to_dict()
    assert blob["available_models"] == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]
    restored = Account.from_dict(blob)
    assert restored.available_models == [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ]


def test_account_round_trips_missing_available_models_as_none():
    """Older account JSON written before this field existed must still load."""
    account = _oauth_account("uuid1")
    blob = account.to_dict()
    blob.pop("available_models", None)  # simulate old format
    restored = Account.from_dict(blob)
    assert restored.available_models is None


def test_model_gated_routing_requires_confirmed_support():
    """Only the discovered account is eligible; the undiscovered one is skipped."""
    discovered = _oauth_account("disc", available_models=["claude-opus-4-8"])
    discovered.status = AccountStatus.VALID
    undiscovered = _oauth_account("undisc", available_models=None)
    undiscovered.status = AccountStatus.VALID

    chosen = asyncio.run(
        _manager(discovered, undiscovered).get_account_for_oauth(
            model="claude-opus-4-8"
        )
    )
    assert chosen.organization_uuid == "disc"


def test_undiscovered_account_not_routed_model_gated():
    """No confirmed account for the model -> raise instead of guessing."""
    undiscovered = _oauth_account("undisc", available_models=None)
    undiscovered.status = AccountStatus.VALID

    with pytest.raises(NoAccountsAvailableError):
        asyncio.run(
            _manager(undiscovered).get_account_for_oauth(model="claude-opus-4-8")
        )


def test_unmodelled_request_serves_any_valid_account():
    """model=None has no discovery requirement."""
    undiscovered = _oauth_account("undisc", available_models=None)
    undiscovered.status = AccountStatus.VALID

    chosen = asyncio.run(_manager(undiscovered).get_account_for_oauth())
    assert chosen.organization_uuid == "undisc"


def test_periodic_loop_discovers_stale_persisted_account(monkeypatch):
    """A pre-existing account loaded with available_models=None must recover via
    the background loop alone — no manual refresh — or it stays unroutable."""
    acct = _oauth_account("old", available_models=None)
    acct.status = AccountStatus.VALID
    acct.oauth_token = OAuthToken("a", "r", 9999999999.0)  # healthy, not near expiry
    mgr = _manager(acct)
    mgr.save_accounts = lambda: None

    async def fake_fetch(account):
        return ["claude-opus-4-8"]

    monkeypatch.setattr(
        account_service.oauth_authenticator, "fetch_available_models", fake_fetch
    )

    async def run():
        # model-gated routing fails before discovery has run
        with pytest.raises(NoAccountsAvailableError):
            await mgr.get_account_for_oauth(model="claude-opus-4-8")
        await mgr._check_and_refresh_accounts()
        for _ in range(5):  # let the scheduled discovery task complete
            await asyncio.sleep(0)
        return await mgr.get_account_for_oauth(model="claude-opus-4-8")

    chosen = asyncio.run(run())
    assert chosen.organization_uuid == "old"
    assert acct.available_models == ["claude-opus-4-8"]
