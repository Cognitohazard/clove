#!/usr/bin/env python3
"""Print the raw upstream reply at every step of the cookie -> OAuth chain.

Runs the steps through the app's own client and helpers -- same TLS fingerprint,
same headers, same PKCE -- so a failure here is the app's failure, not the probe's.
Bodies are printed verbatim on error and withheld on success (they carry account
PII and a live authorization code).

    CLOVE_SK=sk-ant-sid02-... uv run python scripts/probe_oauth.py

Without CLOVE_SK it still probes the token endpoints, which needs no credential:
a live endpoint rejects a bogus grant *semantically* ("invalid_grant"), a retired
one 404s. That alone distinguishes "they moved the URL" from "our payload is wrong".
"""

import asyncio
import base64
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.http_client import create_plain_session, create_session  # noqa: E402
from app.services.oauth import oauth_authenticator  # noqa: E402

TOKEN_URLS = [
    "https://claude.ai/v1/oauth/token",
    "https://api.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]


def _rand() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


async def _body(response) -> str:
    buf = b""
    async for chunk in response.aiter_bytes():
        buf += chunk
    return buf.decode("utf-8", "replace")


def _show(label: str, status: int, body: str) -> None:
    print(f"  {label}\n    HTTP {status}  {body[:400]}\n")


async def probe_token_endpoints() -> None:
    """A bogus grant is safe and diagnostic: alive endpoints answer semantically."""
    print("=== token endpoints (bogus refresh grant) ===")
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": "bogus-probe-token",
        "client_id": settings.oauth_client_id,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/2.1.158 (external, cli)",
    }
    for url in TOKEN_URLS:
        async with create_plain_session(timeout=20, follow_redirects=False) as session:
            try:
                response = await session.request(
                    method="POST", url=url, headers=headers, json=payload
                )
                _show(url, response.status_code, await _body(response))
            except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
                print(f"  {url}\n    EXC {type(exc).__name__}: {exc}\n")


async def probe_cookie_chain(session_key: str) -> None:
    """org info -> authorize -> exchange, stopping at the first step that fails."""
    claude = settings.claude_ai_url.encoded_string().rstrip("/")
    # The app's own header builder, so a drift there shows up here too.
    headers = oauth_authenticator._build_headers(f"sessionKey={session_key}")

    async with create_session(
        timeout=settings.request_timeout, impersonate="chrome", follow_redirects=False
    ) as session:
        print("=== GET /api/organizations ===")
        response = await session.request(
            method="GET", url=f"{claude}/api/organizations", headers=headers
        )
        body = await _body(response)
        if response.status_code != 200:
            _show("organizations", response.status_code, body)
            print("  cookie itself is not usable; nothing downstream can work")
            return
        # A successful body carries the account email and org name; print only the
        # two fields that matter so probe output stays safe to paste into a report.
        print("  organizations\n    HTTP 200 (body withheld: contains account PII)\n")

        orgs = json.loads(body)
        org = next(
            (o for o in orgs if "chat" in (o.get("capabilities") or [])), orgs[0]
        )
        org_uuid = org["uuid"]
        print(f"  org={org_uuid} capabilities={org.get('capabilities')}\n")

        print("=== POST authorize (cookie -> code) ===")
        verifier, challenge = oauth_authenticator._generate_pkce()
        state = _rand()
        response = await session.request(
            method="POST",
            url=settings.oauth_authorize_url.format(organization_uuid=org_uuid),
            headers={**headers, "Content-Type": "application/json"},
            json={
                "response_type": "code",
                "client_id": settings.oauth_client_id,
                "organization_uuid": org_uuid,
                "redirect_uri": settings.oauth_redirect_uri,
                "scope": "user:profile user:inference",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        body = await _body(response)
        if response.status_code != 200:
            _show("authorize", response.status_code, body)
            print(
                "  read error.details.error_code above -- e.g. session_stale_relogin\n"
                "  means the cookie's sign-in is too old; capture a new one right\n"
                "  after signing in."
            )
            return
        # A 200 body embeds a live single-use authorization code in redirect_uri.
        _show("authorize", response.status_code, "OK (authorization code withheld)")

    redirect = json.loads(body).get("redirect_uri", "")
    params = parse_qs(urlparse(redirect).query)
    code = params.get("code", [""])[0]
    print(f"=== POST {settings.oauth_token_url} (code -> token) ===")
    async with create_plain_session(timeout=20, follow_redirects=False) as session:
        response = await session.request(
            method="POST",
            url=settings.oauth_token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "claude-cli/2.1.158 (external, cli)",
            },
            data={
                "code": code,
                "grant_type": "authorization_code",
                "client_id": settings.oauth_client_id,
                "redirect_uri": settings.oauth_redirect_uri,
                "code_verifier": verifier,
                "state": params.get("state", [state])[0],
            },
        )
        body = await _body(response)
        # Don't print real tokens; the status and any error body are the useful part.
        if response.status_code == 200:
            _show("token exchange", response.status_code, "OK (token withheld)")
        else:
            _show("token exchange", response.status_code, body)


async def main() -> None:
    await probe_token_endpoints()
    session_key = os.environ.get("CLOVE_SK")
    if not session_key:
        print("CLOVE_SK not set — skipping the cookie chain.")
        return
    await probe_cookie_chain(session_key)


if __name__ == "__main__":
    asyncio.run(main())
