"""The OAuth identity's persisted parts: ``OAuthToken.account_uuid`` and ``Account.device_id``.

The account uuid lives on the token so that replacing the token can never pair it
with another user's uuid; the device id is generated once per account and must
survive restarts, including for ``accounts.json`` files written before it existed.
"""

import json
import re

import app.services.account as account_service
from app.core.account import Account, AccountStatus, AuthType, OAuthToken
from app.services.account import AccountManager

_TOKEN_RESPONSE = {
    "token_type": "Bearer",
    "access_token": "sk-ant-oat01-new",
    "refresh_token": "sk-ant-ort01-new",
    "expires_in": 28800,
    "account": {"uuid": "8a1f6c1e-0000-4000-8000-000000000001"},
}


def test_from_token_response_takes_the_account_uuid():
    token = OAuthToken.from_token_response(_TOKEN_RESPONSE)
    assert token.account_uuid == "8a1f6c1e-0000-4000-8000-000000000001"
    assert token.access_token == "sk-ant-oat01-new"
    assert token.expires_at > 28800


def test_refresh_without_account_keeps_the_previous_uuid():
    previous = OAuthToken("a", "r", 0.0, account_uuid="kept")
    refreshed = {k: v for k, v in _TOKEN_RESPONSE.items() if k != "account"}
    assert OAuthToken.from_token_response(refreshed, previous).account_uuid == "kept"
    # A response that does name the account wins over the previous token.
    assert (
        OAuthToken.from_token_response(_TOKEN_RESPONSE, previous).account_uuid
        == "8a1f6c1e-0000-4000-8000-000000000001"
    )


def test_pasted_token_has_no_account_uuid():
    pasted = {"access_token": "a", "refresh_token": "r", "expires_at": 123.0}
    token = OAuthToken.from_token_response(pasted)
    assert token.account_uuid == ""
    assert token.expires_at == 123.0


def test_token_round_trips_and_tolerates_legacy_dicts():
    token = OAuthToken("a", "r", 1.0, account_uuid="u")
    assert OAuthToken.from_dict(token.to_dict()) == token
    legacy = {"access_token": "a", "refresh_token": "r", "expires_at": 1.0}
    assert OAuthToken.from_dict(legacy).account_uuid == ""


def test_device_id_is_generated_and_round_trips():
    account = Account(organization_uuid="org", auth_type=AuthType.OAUTH_ONLY)
    assert re.fullmatch(r"[0-9a-f]{64}", account.device_id)
    assert Account(organization_uuid="other").device_id != account.device_id
    assert Account.from_dict(account.to_dict()).device_id == account.device_id


def _legacy_account_dict(org: str) -> dict:
    """An accounts.json entry written before device_id / account_uuid existed."""
    return {
        "organization_uuid": org,
        "capabilities": ["chat"],
        "cookie_value": None,
        "status": AccountStatus.VALID.value,
        "auth_type": AuthType.OAUTH_ONLY.value,
        "last_used": "2026-01-01T00:00:00",
        "resets_at": None,
        "oauth_token": {"access_token": "a", "refresh_token": "r", "expires_at": 1.0},
    }


def _manager() -> AccountManager:
    # AccountManager.__new__ hands back the process-wide singleton; bypass it.
    mgr = object.__new__(AccountManager)
    mgr._accounts = {}
    mgr._cookie_to_uuid = {}
    return mgr


def test_legacy_accounts_file_loads_and_saves_generated_ids_once(tmp_path, monkeypatch):
    monkeypatch.setattr(account_service.settings, "data_folder", tmp_path)
    monkeypatch.setattr(account_service.settings, "no_filesystem_mode", False)
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps({"org": _legacy_account_dict("org")}))

    mgr = _manager()
    saves = []
    real_save = mgr.save_accounts
    monkeypatch.setattr(mgr, "save_accounts", lambda: (saves.append(1), real_save()))
    mgr.load_accounts()

    account = mgr._accounts["org"]
    assert account.oauth_token.account_uuid == ""
    assert len(saves) == 1
    persisted = json.loads(accounts_file.read_text())["org"]
    assert persisted["device_id"] == account.device_id
    assert persisted["oauth_token"]["account_uuid"] == ""

    # The next start finds the id on disk: same device, nothing to save.
    again = _manager()
    monkeypatch.setattr(again, "save_accounts", lambda: saves.append(1))
    again.load_accounts()
    assert again._accounts["org"].device_id == account.device_id
    assert len(saves) == 1
