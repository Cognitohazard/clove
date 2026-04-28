import json

from app.core.http_client import (
    Response,
    AsyncSession,
    create_session,
    RequestException,
)
from datetime import datetime, timedelta, UTC
from typing import Dict, Optional
from loguru import logger
from fastapi.responses import StreamingResponse

from app.models.claude import MessagesAPIRequest
from app.core.observability import current_span
from app.core.observability.usage_tap import create_usage_tap
from app.processors.base import BaseProcessor
from app.processors.claude_ai import ClaudeAIContext
from app.services.account import account_manager
from app.services.cache import cache_service
from app.services.proxy import proxy_service
from app.core.exceptions import (
    ClaudeHttpError,
    ClaudeRateLimitedError,
    InvalidModelNameError,
    NoAccountsAvailableError,
    OAuthAuthenticationNotAllowedError,
    ProxyConnectionError,
)
from app.core.config import settings
from app.utils.oauth_headers import build_oauth_headers


class ClaudeAPIProcessor(BaseProcessor):
    """Processor that calls Claude Messages API directly using OAuth authentication."""

    LEGACY_CLAUDE_CODE_SYSTEM_PROMPT = (
        "You are Claude Code, Anthropic's official CLI for Claude."
    )

    def __init__(self):
        self.messages_api_url = (
            settings.claude_api_baseurl.encoded_string().rstrip("/") + "/v1/messages"
        )

    async def _request_messages_api(
        self, session: AsyncSession, request_body: bytes, headers: Dict[str, str]
    ) -> Response:
        """Make a single upstream Messages API request."""
        return await session.request(
            "POST",
            self.messages_api_url,
            data=request_body,
            headers=headers,
            stream=True,
        )

    async def process(self, context: ClaudeAIContext) -> ClaudeAIContext:
        """
        Process Claude API request using OAuth authentication.

        Requires:
            - messages_api_request in context

        Produces:
            - response in context (StreamingResponse)
        """
        if not context.messages_api_request:
            logger.warning(
                "Skipping ClaudeAPIProcessor due to missing messages_api_request"
            )
            return context

        try:
            # First try to get account from cache service
            cached_account_id, checkpoints = cache_service.process_messages(
                context.messages_api_request.model,
                context.messages_api_request.messages,
                context.messages_api_request.system,
            )

            account = None
            if cached_account_id:
                account = await account_manager.get_account_by_id(cached_account_id)
                if account:
                    logger.info(f"Using cached account: {cached_account_id[:8]}...")

            if not account:
                account = await account_manager.get_account_for_oauth(
                    model=context.messages_api_request.model,
                )

            span = current_span()
            if span is not None:
                span.set_upstream("oauth", account_id=account.organization_uuid)

            with account:
                request_body = await self._prepare_request_body(context)
                headers = self._prepare_headers(
                    account.oauth_token.access_token,
                    context.messages_api_request,
                    context.original_request,
                )

                # Get proxy URL from proxy service
                proxy_url = await proxy_service.get_proxy(
                    account_id=account.organization_uuid
                )

                session: Optional[AsyncSession] = None

                async def close_session() -> None:
                    if session:
                        await session.close()

                try:
                    session = create_session(
                        proxy=proxy_url,
                        timeout=settings.request_timeout,
                        impersonate="chrome",
                        follow_redirects=False,
                        request_retries=1,
                    )

                    response = await self._request_messages_api(
                        session, request_body, headers
                    )
                except RequestException as e:
                    # Mark proxy transport failures as retryable for non-transparent callers.
                    await close_session()
                    if proxy_url:
                        await proxy_service.mark_unhealthy(
                            proxy_url, reason=f"connection error: {type(e).__name__}"
                        )
                    raise ProxyConnectionError(
                        proxy_url=proxy_url,
                        error_type=type(e).__name__,
                    )

                resets_at = response.headers.get("anthropic-ratelimit-unified-reset")
                if resets_at:
                    try:
                        resets_at = int(resets_at)
                        account.resets_at = datetime.fromtimestamp(resets_at, tz=UTC)
                    except ValueError:
                        logger.error(
                            f"Invalid resets_at format from Claude API: {resets_at}"
                        )
                        account.resets_at = None

                # Handle rate limiting
                if response.status_code == 429:
                    next_hour = datetime.now(UTC).replace(
                        minute=0, second=0, microsecond=0
                    ) + timedelta(hours=1)
                    await close_session()
                    raise ClaudeRateLimitedError(
                        resets_at=account.resets_at or next_hour
                    )

                if response.status_code >= 400:
                    # Try to parse error response
                    try:
                        error_data = await response.json()
                        error_type = error_data.get("error", {}).get("type", "unknown")
                        error_message = error_data.get("error", {}).get(
                            "message", "Unknown error"
                        )
                    except Exception:
                        # Empty or invalid JSON response
                        error_data = {}
                        error_type = "empty_response"
                        error_message = (
                            f"HTTP {response.status_code} error with empty response"
                        )

                    # HTTP 403 with empty response and proxy: likely IP banned
                    if (
                        response.status_code == 403
                        and proxy_url
                        and error_type == "empty_response"
                    ):
                        await proxy_service.mark_unhealthy(
                            proxy_url,
                            reason="HTTP 403 Forbidden (empty response) - likely IP banned by Claude API",
                        )

                    if (
                        response.status_code == 400
                        and error_message == "system: Invalid model name"
                    ):
                        await close_session()
                        raise InvalidModelNameError(context.messages_api_request.model)

                    if (
                        response.status_code == 401
                        and error_message
                        == "OAuth authentication is currently not allowed for this organization."
                    ):
                        await close_session()
                        raise OAuthAuthenticationNotAllowedError()

                    logger.error(
                        f"Claude API error: {response.status_code} - {error_data}"
                    )
                    # invalid_request_error 是请求本身有问题，重试不会改变结果
                    await close_session()
                    raise ClaudeHttpError(
                        url=self.messages_api_url,
                        status_code=response.status_code,
                        error_type=error_type,
                        error_message=error_message,
                        retryable=error_type != "invalid_request_error",
                    )

                tap = create_usage_tap(span, response.headers.get("content-type", ""))

                async def stream_response():
                    try:
                        async for chunk in response.aiter_bytes():
                            tap.feed(chunk)
                            yield chunk
                    finally:
                        await tap.close()
                        await close_session()

                filtered_headers = {}
                for key, value in response.headers.items():
                    if key.lower() in ["content-encoding", "content-length"]:
                        logger.debug(f"Filtering out header: {key}: {value}")
                        continue
                    filtered_headers[key] = value

                context.response = StreamingResponse(
                    stream_response(),
                    status_code=response.status_code,
                    headers=filtered_headers,
                )

                logger.info("Successfully processed request via Claude API")

                # Store checkpoints in cache service after successful request
                if checkpoints:
                    cache_service.add_checkpoints(
                        checkpoints, account.organization_uuid
                    )

        except NoAccountsAvailableError:
            logger.debug("No OAuth accounts available, falling back to Web pipeline")
        except InvalidModelNameError as e:
            model_name = e.context.get("model_name") if e.context else None
            logger.debug(
                f"OAuth upstream rejected model {model_name!r}, falling back to Web pipeline"
            )

        return context

    async def _prepare_request_body(self, context: ClaudeAIContext) -> bytes:
        """Prepare the request body for the Claude API.

        Forwards raw bytes by default (transparency). If Claude-Code system
        prompt injection is enabled, patches the cached raw JSON in-place and
        re-serialises — no Pydantic round-trip, so unknown upstream fields
        survive untouched.
        """
        if context.view is None:
            return await context.original_request.body()
        if not settings.inject_claude_code_system_prompt:
            return context.view.raw_body

        data = dict(context.view.raw_json)
        system_block = {"type": "text", "text": self.LEGACY_CLAUDE_CODE_SYSTEM_PROMPT}
        system = data.get("system")
        if isinstance(system, str) and system:
            data["system"] = [system_block, {"type": "text", "text": system}]
        elif isinstance(system, list) and system:
            first = system[0]
            already_injected = (
                isinstance(first, dict)
                and first.get("text") == self.LEGACY_CLAUDE_CODE_SYSTEM_PROMPT
            )
            if not already_injected:
                data["system"] = [system_block] + system
        else:
            data["system"] = [system_block]

        return json.dumps(data, ensure_ascii=False).encode()

    def _prepare_headers(
        self,
        access_token: str,
        request: MessagesAPIRequest,
        original_request=None,
    ) -> Dict[str, str]:
        client_beta = (
            original_request.headers.get("anthropic-beta") if original_request else None
        )
        return build_oauth_headers(
            access_token,
            client_beta_header=client_beta,
            content_type="application/json",
        )
