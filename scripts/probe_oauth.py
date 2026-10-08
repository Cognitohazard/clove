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

``--messages`` is the real-token smoke of the Claude Code identity layer (required
before merging changes to it). It sends through the same transport and builders as
the OAuth path and exits non-zero on any failure:

    CLOVE_OAT=sk-ant-oat01-... uv run python scripts/probe_oauth.py --messages

- non-streaming and streaming 200 on a version-gated model (``--model``);
- a second turn over a cached system prefix reads the cache
  (``cache_read_input_tokens > 0``) although its billing block differs;
- ``GET /v1/models`` and the app's model discovery.
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.account import Account, AuthType, OAuthToken  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.http_client import (  # noqa: E402
    create_oauth_session,
    create_plain_session,
    create_session,
)
from app.services.oauth import oauth_authenticator  # noqa: E402
from app.services.proxy import proxy_service  # noqa: E402
from app.utils.claude_code_identity import (  # noqa: E402
    build_api_headers,
    build_messages_headers,
    bump_cli_version,
    cli_version,
    finalize_messages_body,
    parse_version_gate,
    pin_cli_version,
)

TOKEN_URLS = [
    "https://claude.ai/v1/oauth/token",
    "https://api.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]


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
        verifier, challenge, state = oauth_authenticator.generate_pkce()
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


def _check(label: str, ok: bool, detail: str) -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: {detail}")
    return ok


class _MessagesProbe:
    """Sends /v1/messages the way the OAuth path does, with a throwaway device."""

    def __init__(self, token: str, session):
        self.token = token
        self.session = session
        self.device_id = secrets.token_hex(32)
        base = settings.claude_api_baseurl.encoded_string().rstrip("/")
        self.url = f"{base}/v1/messages?beta=true"

    async def send(self, body: dict, prev_request_id=None):
        """(status, request-id, body text); a version-gate 400 bumps and retries once."""
        for attempt in (1, 2):
            finalized = finalize_messages_body(
                body,
                device_id=self.device_id,
                account_uuid="",
                prev_request_id=prev_request_id,
            )
            headers = build_messages_headers(
                self.token,
                client_beta_header=None,
                session_id=finalized.session_id,
                prompt_id=finalized.prompt_id,
                cli_version=finalized.cli_version,
            )
            response = await self.session.request(
                "POST", self.url, headers=headers, data=finalized.body_bytes
            )
            text = await _body(response)
            gate = parse_version_gate(text) if response.status_code == 400 else None
            if (
                attempt == 1
                and gate
                and (bump_cli_version(gate) or cli_version() != finalized.cli_version)
            ):
                print(f"  version gate: {text[:200]} -> retrying as {cli_version()}")
                continue
            return response.status_code, response.headers.get("request-id"), text


def _usage(text: str) -> dict:
    try:
        return json.loads(text).get("usage") or {}
    except ValueError:
        return {}


async def probe_messages(token: str, model: str) -> bool:
    if settings.claude_code_version:
        pin_cli_version(settings.claude_code_version)
    proxy = await proxy_service.get_proxy()
    print(
        f"=== OAuth /v1/messages as Claude Code {cli_version()} "
        f"(model {model}, proxy {proxy or 'none'}) ==="
    )
    probe = _MessagesProbe(token, create_oauth_session(proxy=proxy))
    hello = [{"role": "user", "content": "Say hi in one word."}]
    ok = True

    status, _, text = await probe.send(
        {"model": model, "max_tokens": 16, "messages": hello}
    )
    ok &= _check(
        "non-streaming",
        status == 200,
        f"HTTP {status} {text[:300] if status != 200 else ''}",
    )

    status, _, text = await probe.send(
        {"model": model, "max_tokens": 16, "stream": True, "messages": hello}
    )
    ok &= _check(
        "streaming",
        status == 200 and "message_stop" in text,
        f"HTTP {status} {text[:300] if status != 200 else 'message_stop seen'}",
    )

    # A fresh prefix well above every model's minimum cacheable length. Turn 2
    # changes the billing block (new prompt id, cch, cc_prev_req) but not the prefix.
    nonce = secrets.token_hex(8)
    prefix = "".join(
        f"Cache probe {nonce}, line {i}: this system prompt stays identical across turns.\n"
        for i in range(600)
    )
    system = [{"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}]
    turn1 = {"model": model, "max_tokens": 16, "system": system, "messages": hello}
    status, request_id, text = await probe.send(turn1)
    ok &= _check(
        "cache turn 1",
        status == 200,
        f"HTTP {status} usage={_usage(text) if status == 200 else text[:300]}",
    )
    turn2 = {
        **turn1,
        "messages": hello
        + [
            {"role": "assistant", "content": "Hi."},
            {"role": "user", "content": "Now say bye in one word."},
        ],
    }
    status, _, text = await probe.send(turn2, prev_request_id=request_id)
    usage = _usage(text)
    ok &= _check(
        "cache turn 2 reads the prefix",
        status == 200 and (usage.get("cache_read_input_tokens") or 0) > 0,
        f"HTTP {status} usage={usage if status == 200 else text[:300]}",
    )

    base = settings.claude_api_baseurl.encoded_string().rstrip("/")
    response = await probe.session.request(
        "GET",
        f"{base}/v1/models",
        headers=build_api_headers(token, cli_version=cli_version()),
        params={"limit": "1000"},
    )
    text = await _body(response)
    ids = [m.get("id") for m in _json_list(text)]
    ok &= _check(
        "GET /v1/models",
        response.status_code == 200 and bool(ids),
        f"HTTP {response.status_code} {len(ids)} models, {model} listed: {model in ids}",
    )

    account = Account(
        organization_uuid="probe",
        oauth_token=OAuthToken(token, "", 0.0),
        auth_type=AuthType.OAUTH_ONLY,
    )
    discovered = await oauth_authenticator.fetch_available_models(account)
    ok &= _check(
        "model discovery",
        bool(discovered),
        f"{len(discovered or [])} models",
    )
    return ok


def _json_list(text: str) -> list:
    try:
        data = json.loads(text).get("data")
    except (ValueError, AttributeError):
        return []
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--messages",
        action="store_true",
        help="real-token smoke of the OAuth path; needs CLOVE_OAT",
    )
    parser.add_argument("--model", default="claude-opus-5-5")
    args = parser.parse_args()

    if args.messages:
        token = os.environ.get("CLOVE_OAT")
        if not token:
            print("CLOVE_OAT is not set (an sk-ant-oat01-... access token).")
            return 2
        return 0 if await probe_messages(token, args.model) else 1

    await probe_token_endpoints()
    session_key = os.environ.get("CLOVE_SK")
    if not session_key:
        print("CLOVE_SK not set — skipping the cookie chain.")
        return 0
    await probe_cookie_chain(session_key)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
