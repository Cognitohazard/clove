"""Three-phase orchestrator for /v1/messages: pre-handlers, strategy, post-chain.

Pre-handlers may short-circuit (tavern canned response) or seed
``original_stream`` (tool-result resumption). Strategy tries OAuth then falls
through to Web. The post-chain runs whenever an SSE ``original_stream`` exists
and translates it into the Anthropic Messages API response shape.
"""

from typing import List, Optional

from loguru import logger

from app.core.observability import current_span
from app.processors.base import BaseProcessor
from app.processors.claude_ai import ClaudeAIContext
from app.processors.claude_ai.claude_api_processor import ClaudeAPIProcessor
from app.processors.claude_ai.claude_web_processor import ClaudeWebProcessor
from app.processors.claude_ai.event_parser_processor import EventParsingProcessor
from app.processors.claude_ai.message_collector_processor import (
    MessageCollectorProcessor,
)
from app.processors.claude_ai.model_injector_processor import ModelInjectorProcessor
from app.processors.claude_ai.non_streaming_response_processor import (
    NonStreamingResponseProcessor,
)
from app.processors.claude_ai.stop_sequences_processor import StopSequencesProcessor
from app.processors.claude_ai.streaming_response_processor import (
    StreamingResponseProcessor,
)
from app.processors.claude_ai.tavern_test_message_processor import TestMessageProcessor
from app.processors.claude_ai.token_counter_processor import TokenCounterProcessor
from app.processors.claude_ai.tool_call_event_processor import ToolCallEventProcessor
from app.processors.claude_ai.tool_result_processor import ToolResultProcessor
from app.services.session import session_manager


class MessagesHandler:
    """Three-phase orchestrator for the Messages API."""

    def __init__(
        self,
        *,
        test_message: Optional[BaseProcessor] = None,
        tool_result: Optional[BaseProcessor] = None,
        oauth: Optional[BaseProcessor] = None,
        web: Optional[BaseProcessor] = None,
        post_processors: Optional[List[BaseProcessor]] = None,
    ):
        self._test_message = test_message or TestMessageProcessor()
        self._tool_result = tool_result or ToolResultProcessor()
        self._oauth = oauth or ClaudeAPIProcessor()
        self._web = web or ClaudeWebProcessor()
        self._post_chain: List[BaseProcessor] = (
            post_processors
            if post_processors is not None
            else [
                EventParsingProcessor(),
                ModelInjectorProcessor(),
                StopSequencesProcessor(),
                ToolCallEventProcessor(),
                MessageCollectorProcessor(),
                TokenCounterProcessor(),
                StreamingResponseProcessor(),
                NonStreamingResponseProcessor(),
            ]
        )

    async def handle(self, context: ClaudeAIContext) -> ClaudeAIContext:
        span = current_span()
        if span is not None and context.view is not None:
            span.set_model(context.view.model, stream=context.view.stream)

        try:
            await self._test_message.process(context)
            if context.response is not None:
                return context

            await self._tool_result.process(context)

            if context.original_stream is None:
                await self._oauth.process(context)
                if context.response is not None:
                    return context

                await self._web.process(context)

            for processor in self._post_chain:
                await processor.process(context)

            return context
        except Exception as exc:
            if context.claude_session:
                await session_manager.remove_session(
                    context.claude_session.session_id
                )
            logger.error(f"Messages handler failed: {exc}")
            raise
