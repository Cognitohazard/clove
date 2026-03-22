import base64
import hashlib
import secrets
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_fixed

from app.core.http_client import Response, create_session, ProxyNetworkException
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
from app.utils.retry import is_retryable_error, log_before_sleep


class RefreshResult(Enum):
    SUCCESS = "success"
    TRANSIENT_ERROR = "transient_error"  # 429, 5xx, network errors
    PERMANENT_ERROR = "permanent_error"  # 401, 403, invalid token


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
        """Build request headers."""
        claude_endpoint = settings.claude_ai_url.encoded_string().rstrip("/")

        return {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Cookie": cookie,
            "Origin": claude_endpoint,
            "Referer": f"{claude_endpoint}/new",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
            try:
                error_data = await response.json()
                error_body = error_data.get("error", {})
                error_message = error_body.get("message", "Unknown error")
                error_type = error_body.get("type", "unknown")
            except Exception:
                error_message = "HTTP 403 error with empty response"
                error_type = "empty_response"

            # 真正的认证错误（有明确的错误消息）
            if error_message == "Invalid authorization":
                raise ClaudeAuthenticationError()

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
            raise ClaudeAuthenticationError()

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

    async def exchange_token(self, code: str, verifier: str) -> Dict:
        """Exchange authorization code for access token."""
        parts = code.split("#")
        auth_code = parts[0]
        state = parts[1] if len(parts) > 1 else None

        data = {
            "code": auth_code,
            "grant_type": "authorization_code",
            "client_id": settings.oauth_client_id,
            "redirect_uri": settings.oauth_redirect_uri,
            "code_verifier": verifier,
        }

        if state:
            data["state"] = state

        try:
            response = await self._request(
                "POST",
                settings.oauth_token_url,
                json=data,
                headers={"Content-Type": "application/json"},
            )

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
            response = await self._request(
                "POST",
                settings.oauth_token_url,
                json=data,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code}")
                return None

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
            # Use _request directly with account_id for proxy consistency
            response = await self._request(
                "POST",
                settings.oauth_token_url,
                account_id=account.organization_uuid,
                json=data,
                headers={"Content-Type": "application/json"},
            )

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.status_code}")
                if response.status_code in (429, 500, 502, 503, 504):
                    return RefreshResult.TRANSIENT_ERROR
                return RefreshResult.PERMANENT_ERROR

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


oauth_authenticator = OAuthAuthenticator()
