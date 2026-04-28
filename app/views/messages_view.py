"""Two-tier view over a Messages API request.

The OAuth path is a transparent proxy: it forwards raw bytes upstream and only
needs a few routing fields (model, messages, system) for cache and account
selection. Pydantic-validating the full body for that path is wasted work and
risks rejecting upstream-valid shapes at the route boundary.

The Web fallback path needs typed access to fields like `thinking`, `tools`,
`tool_choice` to translate the request into Claude.ai's native shape, so it
opts in to the full ``MessagesAPIRequest`` parse via the ``parsed`` property.

Keep cheap accessors strictly raw-JSON-backed — never trigger Pydantic from
``model``/``stream``/``messages``/``system``/``stop_sequences``. That is the
contract that lets the OAuth path stay schema-free.
"""

from __future__ import annotations

import json
from typing import Any, List, Optional

from app.models.claude import MessagesAPIRequest


class MessagesRequestView:
    """Lazy view over a /v1/messages request body.

    Exposes a small set of routing fields directly from raw JSON so the OAuth
    path never pays for a Pydantic round-trip, plus a ``parsed`` property for
    the Web path that needs typed field access.
    """

    __slots__ = ("_raw_body", "_raw_json", "_parsed")

    def __init__(self, raw_body: bytes):
        self._raw_body = raw_body
        self._raw_json: Optional[dict] = None
        self._parsed: Optional[MessagesAPIRequest] = None

    @property
    def raw_body(self) -> bytes:
        return self._raw_body

    @property
    def raw_json(self) -> dict:
        if self._raw_json is None:
            parsed = json.loads(self._raw_body) if self._raw_body else {}
            self._raw_json = parsed if isinstance(parsed, dict) else {}
        return self._raw_json

    @property
    def parsed(self) -> MessagesAPIRequest:
        """Fully parsed request. Triggers Pydantic validation on first access.

        Only the Web path should reach for this. Doing so from the OAuth path
        defeats the transparent-proxy guarantee.
        """
        if self._parsed is None:
            self._parsed = MessagesAPIRequest.model_validate(self.raw_json)
        return self._parsed

    @property
    def model(self) -> str:
        return self.raw_json.get("model") or ""

    @property
    def stream(self) -> bool:
        return bool(self.raw_json.get("stream", False))

    @property
    def messages(self) -> List[Any]:
        return self.raw_json.get("messages") or []

    @property
    def system(self) -> Any:
        return self.raw_json.get("system")

    @property
    def stop_sequences(self) -> Optional[List[str]]:
        return self.raw_json.get("stop_sequences")
