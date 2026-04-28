"""MessagesRequestView lazy parsing contract.

The view backs the OAuth transparent-proxy guarantee: cheap accessors must
*not* trigger Pydantic validation. Only ``parsed`` may.
"""

import json

import pytest

from app.views.messages_view import MessagesRequestView


def _payload() -> bytes:
    return json.dumps(
        {
            "model": "claude-opus-4-7",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "system": "you are helpful",
            "stop_sequences": ["STOP"],
            # An unknown top-level field — extra="allow" keeps it; the view
            # exposes it via raw_json without validating against the schema.
            "service_tier": "standard_only",
        }
    ).encode()


def test_cheap_accessors_do_not_trigger_parsing():
    view = MessagesRequestView(_payload())
    # Touching every cheap accessor must not realize the parsed Pydantic model.
    _ = view.model
    _ = view.stream
    _ = view.messages
    _ = view.system
    _ = view.stop_sequences
    _ = view.raw_json
    _ = view.raw_body
    assert view._parsed is None


def test_parsed_is_lazy_and_cached():
    view = MessagesRequestView(_payload())
    first = view.parsed
    second = view.parsed
    assert first is second  # cached
    assert first.model == "claude-opus-4-7"


def test_cheap_accessors_match_raw_values():
    view = MessagesRequestView(_payload())
    assert view.model == "claude-opus-4-7"
    assert view.stream is True
    assert view.messages == [{"role": "user", "content": "hi"}]
    assert view.system == "you are helpful"
    assert view.stop_sequences == ["STOP"]


def test_empty_body_yields_safe_defaults():
    view = MessagesRequestView(b"")
    assert view.model == ""
    assert view.stream is False
    assert view.messages == []
    assert view.system is None
    assert view.stop_sequences is None


def test_non_object_body_yields_empty_dict():
    """Defensive: a JSON array or scalar shouldn't blow up cheap accessors."""
    view = MessagesRequestView(b"[]")
    assert view.raw_json == {}
    assert view.model == ""


def test_parsed_raises_on_structural_invalid():
    view = MessagesRequestView(b'{"model":"x"}')  # missing messages
    with pytest.raises(Exception):
        _ = view.parsed
