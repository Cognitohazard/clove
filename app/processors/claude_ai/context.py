from dataclasses import dataclass
from typing import Optional, AsyncIterator

from app.core.claude_session import ClaudeWebSession
from app.models.claude import Message, MessagesAPIRequest
from app.models.internal import ClaudeWebRequest
from app.models.streaming import StreamingEvent
from app.processors.base import BaseContext
from app.views.messages_view import MessagesRequestView


@dataclass
class ClaudeAIContext(BaseContext):
    view: Optional[MessagesRequestView] = None
    claude_web_request: Optional[ClaudeWebRequest] = None
    claude_session: Optional[ClaudeWebSession] = None
    original_stream: Optional[AsyncIterator[str]] = None
    event_stream: Optional[AsyncIterator[StreamingEvent]] = None
    collected_message: Optional[Message] = None

    @property
    def messages_api_request(self) -> Optional[MessagesAPIRequest]:
        """Strict-parsed request (lazy via the view).

        Single source of truth: the parsed model is whatever ``view.parsed``
        returns. Kept as a property so existing call sites stay terse.
        """
        return self.view.parsed if self.view is not None else None
