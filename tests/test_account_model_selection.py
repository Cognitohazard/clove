"""Account-side support for model-aware OAuth selection.

Covers the data plumbing that lets ``AccountManager.get_account_for_oauth``
prefer accounts whose discovered ``/v1/models`` list confirms support for the
requested model, falling back to the static MAX_MODELS heuristic when nothing
has been discovered yet.
"""

from app.core.account import Account, AuthType, AccountStatus, OAuthToken


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


def test_can_serve_model_unknown_returns_none():
    """No discovery yet -> let callers fall back to static heuristic."""
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
