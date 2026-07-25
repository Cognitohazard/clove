import base64
import hashlib
import secrets
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.core.http_client import Response, create_session, create_plain_session, ProxyNetworkException
from loguru import logger

from app.core.config import settings
from app.core.account import Account, AuthType, OAuthToken
from app.core.exceptions import (
    AppError,
    ClaudeAuthenticationError,
    ClaudeHttpError,
    CloudflareBlockedError,
    CookieAuthorizationError,
    OAuthExchangeError,
    OrganizationInfoError,
    ProxyConnectionError,
)
from app.services.proxy import proxy_service
from app.utils.oauth_headers import build_oauth_headers
from app.utils.retry import is_retryable_error, log_before_sleep


class RefreshResult(Enum):
    SUCCESS = "success"
    TRANSIENT_ERROR = "transient_error"  # 429, 5xx, network errors
    PERMANENT_ERROR = "permanent_error"  # 401, 403, invalid token


# Upstream error_code meaning "this cookie's login is too old to grant OAuth access".
# Retrying is futile until the operator captures a cookie from a fresh sign-in.
SESSION_STALE_ERROR_CODE = "session_stale_relogin"


async def _upstream_detail(response: Response) -> str:
    """Best-effort human-readable reason out of an upstream error body.

    Covers both Anthropic's ``{"error": {"type", "message", "details"}}`` envelope
    and the plain OAuth ``{"error", "error_description"}`` shape. Returns "" when
    the body is missing or unparseable — callers supply their own fallback text.
    """
    try:
        body = await response.json()
    except Exception:
        return ""

    if not isinstance(body, dict):
        return ""

    error = body.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("type") or ""
    return " ".join(str(p) for p in (error, body.get("error_description")) if p)


class OAuthAuthenticator:
    """OAuth authenticator for Claude accounts using cookies."""

    def _generate_pkce(self) -> Tuple[str, str]:
        """Generate PKCE verifier and challenge."""
        verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode("utf-8")
            .rstrip("=")
        )
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest())
            .decode("utf-8")
            .rstrip("=")
        )
        return verifier, challenge

    def _build_headers(self, cookie: str) -> Dict[str, str]:
        """Build request headers for browser-impersonating claude.ai calls.

        Deliberately no User-Agent: the rnet Chrome emulation
        (impersonate="chrome") supplies one that matches the TLS fingerprint.
        Don't hardcode a UA here — it would drift from the emulation's JA3.
        (The token endpoint in ``_plain_request`` uses a plain session and
        intentionally sets its own claude-cli UA.)
        """
        claude_endpoint = settings.claude_ai_url.encoded_string().rstrip("/")

        return {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Cookie": cookie,
            "Origin": claude_endpoint,
            "Referer": f"{claude_endpoint}/new",
        }

    @retry(
        retry=retry_if_exception(is_retryable_error),
        stop=stop_after_attempt(settings.retry_attempts),
        wait=wait_fixed(settings.retry_interval),
        before_sleep=log_before_sleep,
        reraise=True,
    )
    async def _request(
        self,
        method: str,
        url: str,
        account_id: Optional[str] = None,
        cookie: Optional[str] = None,
        **kwargs,
    ) -> Response:
        """Browser-impersonating request — for claude.ai endpoints (Cloudflare)."""
        # Get proxy URL from proxy service
        proxy_url = await proxy_service.get_proxy(account_id=account_id, cookie=cookie)

        session = create_session(
            timeout=settings.request_timeout,
            impersonate="chrome",
            proxy=proxy_url,
            follow_redirects=False,
        )
        async with session:
            try:
                response: Response = await session.request(method=method, url=url, **kwargs)
            except ProxyNetworkException as e:
                # Connection error: mark proxy as unhealthy and wrap as retryable AppError
                if proxy_url:
                    await proxy_service.mark_unhealthy(
                        proxy_url,
                        reason=f"connection error: {type(e).__name__}"
                    )
                raise ProxyConnectionError(
                    proxy_url=proxy_url,
                    error_type=type(e).__name__,
                )

        if response.status_code == 302:
            raise CloudflareBlockedError()

        if response.status_code == 403:
            # 先解析响应内容，区分代理问题和认证错误
            upstream_error_code = None
            try:
                error_data = await response.json()
                error_body = error_data.get("error", {})
                error_message = error_body.get("message", "Unknown error")
                error_type = error_body.get("type", "unknown")
                details = error_body.get("details") or {}
                upstream_error_code = details.get("error_code")
            except Exception:
                error_message = "HTTP 403 error with empty response"
                error_type = "empty_response"

            # Carried on the exception (and so into every log of it): a bare
            # ClaudeAuthenticationError makes every 403 look alike.
            auth_context = {
                "upstream_url": url,
                "upstream_type": error_type,
                "upstream_message": error_message,
                "upstream_error_code": upstream_error_code,
            }

            # 真正的认证错误（有明确的错误消息）
            if error_message == "Invalid authorization":
                raise ClaudeAuthenticationError(context=auth_context)

            # 403 + 空响应 + 有代理 = 代理 IP 被封
            if proxy_url and error_type == "empty_response":
                await proxy_service.mark_unhealthy(
                    proxy_url,
                    reason="HTTP 403 Forbidden (empty response) - likely IP banned",
                )
                # 抛出可重试的异常，换代理重试
                raise ClaudeHttpError(
                    url=url,
                    status_code=403,
                    error_type=error_type,
                    error_message=error_message,
                )

            # 其他 403 情况，抛出认证错误
            raise ClaudeAuthenticationError(context=auth_context)

        if response.status_code == 429:
            raise ClaudeHttpError(
                url=url,
                status_code=429,
                error_type="rate_limited",
                error_message="Rate limited by upstream server",
                retryable=False,
            )

        if response.status_code >= 300:
            raise ClaudeHttpError(
                url=url,
                status_code=response.status_code,
                error_type="Unknown",
                error_message="Error occurred during request to Claude.ai",
            )

        return response

    async def _token_request(
        self,
        url: str,
        data: Dict,
        account_id: Optional[str] = None,
    ) -> Response:
        """Plain HTTP request for OAuth token endpoints (console.anthropic.com).

        Uses a non-impersonating HTTP client with form-encoded data and
        claude-cli User-Agent, matching the real Claude CLI behavior.
        """
        proxy_url = await proxy_service.get_proxy(account_id=account_id)

        session = create_plain_session(
            timeout=settings.request_timeout,
            proxy=proxy_url,
            follow_redirects=False,
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "claude-cli/2.1.158 (external, cli)",
        }
        async with session:
            try:
                response: Response = await session.request(
                    method="POST", url=url, headers=headers, data=data,
                )
            except ProxyNetworkException as e:
                if proxy_url:
                    await proxy_service.mark_unhealthy(
                        proxy_url,
                        reason=f"connection error: {type(e).__name__}"
                    )
                raise ProxyConnectionError(
                    proxy_url=proxy_url,
                    error_type=type(e).__name__,
                )

        if response.status_code == 429:
            raise ClaudeHttpError(
                url=url,
                status_code=429,
                error_type="rate_limited",
                error_message="Rate limited by upstream server",
                retryable=False,
            )

        if response.status_code >= 400:
            # The body names the rejected field or grant; without it every token
            # failure reads the same.
            detail = await _upstream_detail(response)
            logger.error(
                f"OAuth token request to {url} failed: "
                f"HTTP {response.status_code} {detail or '(no error body)'}"
            )
            raise ClaudeHttpError(
                url=url,
                status_code=response.status_code,
                error_type="token_error",
                error_message=detail or "OAuth token request failed",
            )

        return response

    async def get_organization_info(self, cookie: str) -> Tuple[str, List[str]]:
        """Get organization UUID and capabilities."""
        url = f"{settings.claude_ai_url.encoded_string().rstrip('/')}/api/organizations"
        headers = self._build_headers(cookie)

        try:
            # Use cookie for proxy selection (no account_id available yet)
            response = await self._request("GET", url, cookie=cookie, headers=headers)

            org_data = await response.json()
            if org_data and isinstance(org_data, list):
                organization_uuid = None
                max_capabilities = []

                for org in org_data:
                    if "uuid" in org and "capabilities" in org:
                        capabilities = org.get("capabilities", [])

                        if "chat" not in capabilities:
                            continue

                        if len(capabilities) > len(max_capabilities):
                            organization_uuid = org.get("uuid")
                            max_capabilities = capabilities

                if organization_uuid:
                    logger.info(
                        f"Found organization UUID: {organization_uuid}, capabilities: {max_capabilities}"
                    )
                    return organization_uuid, max_capabilities

                raise OrganizationInfoError(
                    reason="No valid organization found with chat capabilities"
                )

            else:
                logger.error("No organization data found in response")
                raise OrganizationInfoError(reason="No organization data found")

        except AppError as e:
            raise e

        except Exception as e:
            logger.error(f"Error getting organization UUID: {e}")
            raise OrganizationInfoError(reason=str(e))

    async def authorize_with_cookie(
        self, cookie: str, organization_uuid: str
    ) -> Tuple[str, str]:
        """
        Use Cookie to automatically get authorization code.
        Returns: (authorization code, verifier)
        """
        verifier, challenge = self._generate_pkce()
        state = (
            base64.urlsafe_b64encode(secrets.token_bytes(32))
            .decode("utf-8")
            .rstrip("=")
        )

        authorize_url = settings.oauth_authorize_url.format(
            organization_uuid=organization_uuid
        )

        payload = {
            "response_type": "code",
            "client_id": settings.oauth_client_id,
            "organization_uuid": organization_uuid,
            "redirect_uri": settings.oauth_redirect_uri,
            "scope": "user:profile user:inference",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }

        headers = self._build_headers(cookie)
        headers["Content-Type"] = "application/json"

        logger.debug(f"Requesting authorization from: {authorize_url}")

        # Use organization_uuid for proxy selection
        response = await self._request(
            "POST", authorize_url, account_id=organization_uuid, json=payload, headers=headers
        )

        auth_response = await response.json()
        redirect_uri = auth_response.get("redirect_uri")

        if not redirect_uri:
            logger.error("No redirect_uri in authorization response")
            raise CookieAuthorizationError(reason="No redirect URI found in response")

        logger.info(f"Got redirect URI: {redirect_uri}")

        parsed_url = urlparse(redirect_uri)
        query_params = parse_qs(parsed_url.query)

        if "code" not in query_params:
            logger.error("No authorization code in redirect_uri")
            raise CookieAuthorizationError(
                reason="No authorization code found in response"
            )

        auth_code = query_params["code"][0]
        response_state = query_params.get("state", [None])[0]

        logger.info(f"Extracted authorization code: {auth_code[:20]}...")

        if response_state:
            full_code = f"{auth_code}#{response_state}"
        else:
            full_code = auth_code

        return full_code, verifier

    async def exchange_token(
        self, code: str, verifier: str, state: Optional[str] = None
    ) -> Dict:
        """Exchange authorization code for access token.

        ``state`` is mandatory upstream: omitting it is rejected with a generic
        ``invalid_request_error / "Invalid request format"`` naming no field.
        """
        parts = code.split("#")
        auth_code = parts[0]
        tail = parts[1] if len(parts) > 1 else None
        if not (tail or state):
            # Legacy admin UI reused the verifier as state; remove once no client does.
            logger.warning("Token exchange has no state; falling back to the verifier")
        state = tail or state or verifier

        data = {
            "code": auth_code,
            "grant_type": "authorization_code",
            "client_id": settings.oauth_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "code_verifier": verifier,
            "state": state,
        }

        try:
            response = await self._token_request(settings.oauth_token_url, data=data)

            token_data = await response.json()

            if (
                "access_token" not in token_data
                or "refresh_token" not in token_data
                or "expires_in" not in token_data
            ):
                logger.error("Invalid token response received")
                raise OAuthExchangeError(reason="Invalid token response")

            return token_data

        except AppError as e:
            raise e

        except Exception as e:
            logger.error(f"Error exchanging token: {e}")
            raise OAuthExchangeError(reason=str(e))

    async def refresh_access_token(self, refresh_token: str) -> Optional[Dict]:
        """Refresh access token."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.oauth_client_id,
        }

        try:
            response = await self._token_request(settings.oauth_token_url, data=data)
            token_data = await response.json()
            return token_data

        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            return None

    async def authenticate_account(self, account: Account) -> bool:
        """
        Authenticate an account using OAuth.
        Returns True if successful, False otherwise.
        """
        if not account.cookie_value:
            logger.error("Account has no cookie value")
            return False

        try:
            # Get organization UUID
            org_uuid, _ = await self.get_organization_info(account.cookie_value)

            # Get authorization code
            auth_result = await self.authorize_with_cookie(
                account.cookie_value, org_uuid
            )

            auth_code, verifier = auth_result

            # Exchange for tokens
            token_data = await self.exchange_token(auth_code, verifier)

            # Update account with OAuth tokens
            account.oauth_token = OAuthToken(
                access_token=token_data["access_token"],
                refresh_token=token_data["refresh_token"],
                expires_at=time.time() + token_data["expires_in"],
            )
            account.auth_type = AuthType.BOTH
            account.save()

            logger.info(
                f"Successfully authenticated account with OAuth: {account.organization_uuid[:8]}..."
            )
            return True

        except Exception as e:
            context = getattr(e, "context", None) or {}
            if context.get("upstream_error_code") == SESSION_STALE_ERROR_CODE:
                # Permanent for this cookie, so stop retrying: the periodic loop
                # would otherwise hammer upstream forever with a guaranteed failure.
                account.oauth_upgrade_blocked_reason = SESSION_STALE_ERROR_CODE
                account.save()
                logger.error(
                    f"OAuth upgrade permanently blocked for {account.organization_uuid[:8]}...: "
                    "claude.ai requires a fresh sign-in to grant OAuth access. Sign out and "
                    "back in, then replace this account's cookie with the new sessionKey."
                )
            else:
                logger.error(f"OAuth authentication failed: {e}")
            return False

    async def refresh_account_token(self, account: Account) -> RefreshResult:
        """
        Refresh OAuth token for an account.

        Returns:
            RefreshResult.SUCCESS: Token refreshed successfully
            RefreshResult.PERMANENT_ERROR: Credentials invalid, token should be wiped
            RefreshResult.TRANSIENT_ERROR: Temporary failure (429, 5xx, network), retry later
        """
        if not account.oauth_token or not account.oauth_token.refresh_token:
            logger.error("Account has no refresh token")
            return RefreshResult.PERMANENT_ERROR

        data = {
            "grant_type": "refresh_token",
            "refresh_token": account.oauth_token.refresh_token,
            "client_id": settings.oauth_client_id,
        }

        try:
            response = await self._token_request(
                settings.oauth_token_url,
                data=data,
                account_id=account.organization_uuid,
            )
            token_data = await response.json()

        except AppError as e:
            # 429 is non-retryable (to prevent hammering) but transient for backoff purposes
            is_transient = e.retryable or e.status_code == 429
            if is_transient:
                logger.warning(
                    f"Transient error refreshing token for {account.organization_uuid[:8]}...: {e}"
                )
                return RefreshResult.TRANSIENT_ERROR
            logger.error(
                f"Permanent error refreshing token for {account.organization_uuid[:8]}...: {e}"
            )
            return RefreshResult.PERMANENT_ERROR

        except Exception as e:
            logger.warning(
                f"Unexpected error refreshing token for {account.organization_uuid[:8]}...: {e}"
            )
            return RefreshResult.TRANSIENT_ERROR

        account.oauth_token = OAuthToken(
            access_token=token_data["access_token"],
            refresh_token=token_data["refresh_token"],
            expires_at=time.time() + token_data["expires_in"],
        )
        account.save()

        logger.info(
            f"Successfully refreshed OAuth token for account: {account.organization_uuid[:8]}..."
        )
        return RefreshResult.SUCCESS

    async def fetch_available_models(self, account: Account) -> Optional[List[str]]:
        """Query upstream /v1/models to discover the model IDs this OAuth account can serve.

        Returns:
            A list of model IDs on success, or ``None`` on any failure (network,
            auth, parse). The caller treats ``None`` as "leave the existing
            cached value alone" so a transient outage doesn't wipe knowledge.
        """
        if not account.oauth_token or not account.oauth_token.access_token:
            return None

        url = settings.claude_api_baseurl.encoded_string().rstrip("/") + "/v1/models"
        proxy_url = await proxy_service.get_proxy(account_id=account.organization_uuid)
        session = create_session(
            timeout=settings.request_timeout,
            impersonate="chrome",
            proxy=proxy_url,
            follow_redirects=False,
        )

        try:
            async with session:
                response = await session.request(
                    method="GET",
                    url=url,
                    headers=build_oauth_headers(
                        account.oauth_token.access_token,
                        accept="application/json",
                    ),
                    params={"limit": "1000"},
                )
                if response.status_code != 200:
                    logger.debug(
                        f"Model discovery for {account.organization_uuid[:8]}... "
                        f"got HTTP {response.status_code}; keeping cached list"
                    )
                    return None
                data = await response.json()
        except ProxyNetworkException as exc:
            if proxy_url:
                await proxy_service.mark_unhealthy(
                    proxy_url, reason=f"connection error: {type(exc).__name__}"
                )
            return None
        except Exception as exc:
            logger.debug(
                f"Model discovery for {account.organization_uuid[:8]}... failed: {exc}"
            )
            return None

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None

        model_ids = [
            item["id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return model_ids or None


oauth_authenticator = OAuthAuthenticator()
