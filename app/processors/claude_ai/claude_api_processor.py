import asyncio

from app.core.http_client import (
    OAUTH_TRANSPORT_ERRORS,
    Response,
    create_oauth_session,
)
from datetime import datetime, timedelta, UTC
from typing import Optional
from loguru import logger
from fastapi.responses import StreamingResponse

from app.core.account import Account
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
    MalformedRequestBodyError,
    NoAccountsAvailableError,
    OAuthAuthenticationNotAllowedError,
    ProxyConnectionError,
)
from app.core.config import settings
from app.utils.claude_code_identity import (
    FinalizedBody,
    build_messages_headers,
    bump_cli_version,
    cli_version,
    effective_session_id,
    finalize_messages_body,
    parse_version_gate,
)
from app.views.messages_view import MessagesRequestView

# Finalizing a body costs ~8 ms per 234 KB (~84 ms for a 7 MB image body);
# above this size it runs off the event loop.
_FINALIZE_OFFLOAD_BYTES = 1 << 20
# ponytail: in-memory and per process, capped at 4096 conversations with the
# least recently answered dropped first. A restart or an eviction only costs that
# conversation's next turn its cc_prev_req; persist it if upstream starts caring.
_PREV_REQUEST_IDS_MAX = 4096


async def _read_error(response: Response) -> tuple[dict, str, str]:
    """(body, error type, error message) of an upstream error response."""
    try:
        error_data = await response.json()
        error_type = error_data.get("error", {}).get("type", "unknown")
        error_message = error_data.get("error", {}).get("message", "Unknown error")
    except Exception:
        # Empty or invalid JSON response
        error_data = {}
        error_type = "empty_response"
        error_message = f"HTTP {response.status_code} error with empty response"
    return error_data, error_type, error_message


class ClaudeAPIProcessor(BaseProcessor):
    """Processor that calls Claude Messages API directly using OAuth authentication.

    Every request goes out as the Claude Code CLI would send it: body via
    ``finalize_messages_body``, headers via ``build_messages_headers``, transport
    via ``create_oauth_session``.
    """

    def __init__(self):
        self.messages_api_url = (
            settings.claude_api_baseurl.encoded_string().rstrip("/")
            + "/v1/messages?beta=true"
        )
        # Session id sent on the wire -> request-id of its last successful main
        # response, which the CLI sends back as cc_prev_req.
        self._prev_request_ids: dict[str, str] = {}

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
                # A client's x-claude-code-request-class is never adopted: every
                # request goes out as main, the only class a full billing block
                # and cc_prev_req belong to.
                inbound = context.original_request.headers

                # Get proxy URL from proxy service
                proxy_url = await proxy_service.get_proxy(
                    account_id=account.organization_uuid
                )
                session = create_oauth_session(
                    proxy=proxy_url, timeout=settings.request_timeout
                )

                try:
                    # A version-gate 400 bumps the CLI version and is retried once.
                    for attempt in (1, 2):
                        finalized = await self._finalize(
                            context.view,
                            account,
                            client_prompt_id=inbound.get("x-claude-code-prompt-id"),
                        )
                        headers = build_messages_headers(
                            account.oauth_token.access_token,
                            client_beta_header=inbound.get("anthropic-beta"),
                            session_id=finalized.session_id,
                            prompt_id=finalized.prompt_id,
                            cli_version=finalized.cli_version,
                        )
                        response = await session.request(
                            "POST",
                            self.messages_api_url,
                            headers=headers,
                            data=finalized.body_bytes,
                        )
                        error = None
                        if response.status_code == 400:
                            error = await _read_error(response)
                        gate = (
                            parse_version_gate(error[2])
                            if error and isinstance(error[2], str)
                            else None
                        )
                        if gate:
                            bumped = bump_cli_version(gate)
                            # A concurrent request may already have bumped it.
                            if attempt == 1 and (
                                bumped or cli_version() != finalized.cli_version
                            ):
                                logger.warning(
                                    f"Upstream requires Claude Code {gate} "
                                    f"or newer; retrying as {cli_version()}"
                                )
                                continue
                        break
                except OAUTH_TRANSPORT_ERRORS as e:
                    # Mark proxy transport failures as retryable for non-transparent callers.
                    await session.close()
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
                    await session.close()
                    raise ClaudeRateLimitedError(
                        resets_at=account.resets_at or next_hour
                    )

                if response.status_code >= 400:
                    error_data, error_type, error_message = error or await _read_error(
                        response
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
                        await session.close()
                        raise InvalidModelNameError(context.messages_api_request.model)

                    if (
                        response.status_code == 401
                        and error_message
                        == "OAuth authentication is currently not allowed for this organization."
                    ):
                        await session.close()
                        raise OAuthAuthenticationNotAllowedError()

                    logger.error(
                        f"Claude API error: {response.status_code} - {error_data}"
                    )
                    # invalid_request_error 是请求本身有问题，重试不会改变结果
                    await session.close()
                    raise ClaudeHttpError(
                        url=self.messages_api_url,
                        status_code=response.status_code,
                        error_type=error_type,
                        error_message=error_message,
                        retryable=error_type != "invalid_request_error",
                    )

                self._remember_request_id(
                    finalized.session_id, response.headers.get("request-id")
                )

                tap = create_usage_tap(span, response.headers.get("content-type", ""))

                async def stream_response():
                    try:
                        async for chunk in response.aiter_bytes():
                            tap.feed(chunk)
                            yield chunk
                    finally:
                        await tap.close()
                        await session.close()

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

    async def _finalize(
        self,
        view: MessagesRequestView,
        account: Account,
        *,
        client_prompt_id: Optional[str],
    ) -> FinalizedBody:
        """The CLI-shaped body for the client's raw JSON.

        ``INJECT_CLAUDE_CODE_SYSTEM_PROMPT=false`` omits only the identity block;
        billing and ``metadata.user_id`` always apply. ``cc_prev_req`` is looked
        up by the session id the body is sent with.
        """

        def run() -> FinalizedBody:
            body = view.raw_json
            session_id = effective_session_id(account.device_id, body)
            return finalize_messages_body(
                body,
                device_id=account.device_id,
                account_uuid=account.oauth_token.account_uuid,
                session_id=session_id,
                client_prompt_id=client_prompt_id,
                include_identity=settings.inject_claude_code_system_prompt,
                prev_request_id=self._prev_request_ids.get(session_id),
            )

        try:
            if len(view.raw_body) > _FINALIZE_OFFLOAD_BYTES:
                return await asyncio.to_thread(run)
            return run()
        except ValueError as exc:  # NaN / Infinity, a system of the wrong type
            raise MalformedRequestBodyError(detail=str(exc)) from exc

    def _remember_request_id(self, session_id: str, request_id: Optional[str]) -> None:
        if not request_id:
            return
        ids = self._prev_request_ids
        ids.pop(session_id, None)
        ids[session_id] = request_id
        if len(ids) > _PREV_REQUEST_IDS_MAX:
            del ids[next(iter(ids))]
