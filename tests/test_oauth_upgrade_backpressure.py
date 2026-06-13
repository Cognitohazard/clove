"""Backpressure guarantees for the cookie -> OAuth self-heal path.

These pin the two properties an adversarial review flagged as missing:
  * the global semaphore bounds how many cookie->OAuth upgrades run at once,
    however they were triggered, so a degraded token endpoint or a bulk cookie
    import can't stampede upstream; and
  * a failed background upgrade clears ``is_refreshing`` and arms a *jittered*
    cooldown so a batch of failures doesn't re-synchronize and refire together.
"""

import asyncio
import time
from datetime import datetime, timedelta, UTC

from app.core.account import Account, AuthType, AccountStatus, OAuthToken
from app.services.account import AccountManager
from app.services import oauth as oauth_module


def _cookie_only(org_uuid: str) -> Account:
    acc = Account(
        organization_uuid=org_uuid,
        cookie_value=f"cookie-{org_uuid}",
        auth_type=AuthType.COOKIE_ONLY,
        capabilities=["chat"],
    )
    acc.status = AccountStatus.VALID
    return acc


def _fresh_manager() -> AccountManager:
    # AccountManager is a singleton whose __init__ re-runs each call; reset the
    # mutable state and force a new semaphore bound to the test's event loop.
    mgr = AccountManager()
    mgr._accounts.clear()
    mgr._oauth_upgrade_semaphore = None
    return mgr


async def test_concurrent_cookie_oauth_upgrades_are_bounded(monkeypatch):
    """Many simultaneous upgrades never exceed MAX_CONCURRENT_OAUTH_UPGRADES."""
    mgr = _fresh_manager()
    accounts = [_cookie_only(f"org-{i}") for i in range(12)]

    inflight = 0
    max_inflight = 0

    async def fake_authenticate(_account):
        nonlocal inflight, max_inflight
        inflight += 1  # atomic: no await between here and the max read
        max_inflight = max(max_inflight, inflight)
        try:
            await asyncio.sleep(0.02)  # hold the slot so overlap is observable
        finally:
            inflight -= 1
        return False  # stays cookie-only

    monkeypatch.setattr(
        oauth_module.oauth_authenticator, "authenticate_account", fake_authenticate
    )

    await asyncio.gather(
        *(mgr._attempt_oauth_authentication(a) for a in accounts)
    )

    assert max_inflight >= 1, "sanity: the fake authenticator must have run"
    assert max_inflight <= mgr.MAX_CONCURRENT_OAUTH_UPGRADES


async def test_failed_background_upgrade_arms_jittered_cooldown(monkeypatch):
    """A failed self-heal frees is_refreshing and backs off within the window."""
    mgr = _fresh_manager()
    acc = _cookie_only("org-fail")
    acc.is_refreshing = True

    async def fake_fail(_account):
        return False

    monkeypatch.setattr(
        oauth_module.oauth_authenticator, "authenticate_account", fake_fail
    )

    before = datetime.now(UTC)
    await mgr._upgrade_cookie_only_account(acc)

    assert acc.is_refreshing is False
    assert acc.refresh_retry_after is not None
    low = before + timedelta(seconds=mgr.OAUTH_UPGRADE_RETRY_INTERVAL)
    high = before + timedelta(
        seconds=mgr.OAUTH_UPGRADE_RETRY_INTERVAL + mgr.OAUTH_UPGRADE_RETRY_JITTER + 5
    )
    assert low <= acc.refresh_retry_after <= high


async def test_successful_background_upgrade_clears_cooldown(monkeypatch):
    """A successful self-heal clears the cooldown and the in-flight flag."""
    mgr = _fresh_manager()
    acc = _cookie_only("org-ok")
    acc.is_refreshing = True

    async def fake_success(account):
        account.oauth_token = OAuthToken("a", "r", time.time() + 3600)
        account.auth_type = AuthType.BOTH
        return True

    monkeypatch.setattr(
        oauth_module.oauth_authenticator, "authenticate_account", fake_success
    )

    await mgr._upgrade_cookie_only_account(acc)

    assert acc.is_refreshing is False
    assert acc.auth_type == AuthType.BOTH
    assert acc.refresh_retry_after is None
