"""Contract tests: lock in the proxy's transparency guarantee.

The OAuth path forwards the raw request body to upstream and only relies on
``MessagesAPIRequest`` for cache lookup, account selection, and model routing.
If FastAPI/Pydantic 422s a payload upstream would have accepted, the proxy is
broken regardless of how nice the downstream code looks.

These tests pin the request shapes the live Anthropic Messages API documents,
including ones added since the model was first written. Add a fixture here
whenever upstream introduces a new field, value, or shape — it's cheaper than
finding the regression in production.

Sources for the fixtures (cross-checked 2026-04-28):
  https://platform.claude.com/docs/en/api/messages
  https://platform.claude.com/docs/en/build-with-claude/effort
  https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking
  https://platform.claude.com/docs/en/build-with-claude/extended-thinking
"""

from typing import Any, Dict

import pytest
from pydantic import ValidationError

from app.models.claude import MessagesAPIRequest


def _base() -> Dict[str, Any]:
    return {
        "model": "claude-opus-4-7",
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": "hello"}],
    }


# Each fixture is a (label, payload) pair. The label appears in test IDs so a
# failure points straight at the upstream feature being violated.
ACCEPTED_PAYLOADS = [
    pytest.param(_base(), id="minimal"),
    # --- output_config / effort (GA, 2026) ---
    pytest.param(
        {**_base(), "output_config": {"effort": "low"}},
        id="effort-low",
    ),
    pytest.param(
        {**_base(), "output_config": {"effort": "medium"}},
        id="effort-medium",
    ),
    pytest.param(
        {**_base(), "output_config": {"effort": "high"}},
        id="effort-high",
    ),
    pytest.param(
        {**_base(), "output_config": {"effort": "xhigh"}},
        id="effort-xhigh-opus47",
    ),
    pytest.param(
        {**_base(), "output_config": {"effort": "max"}},
        id="effort-max",
    ),
    pytest.param(
        # Forward-compatibility: any future effort level must pass through.
        {**_base(), "output_config": {"effort": "turbo"}},
        id="effort-future-value",
    ),
    # --- thinking modes ---
    pytest.param(
        {**_base(), "thinking": {"type": "adaptive"}},
        id="thinking-adaptive",
    ),
    pytest.param(
        {**_base(), "thinking": {"type": "adaptive", "display": "summarized"}},
        id="thinking-adaptive-display-summarized",
    ),
    pytest.param(
        {**_base(), "thinking": {"type": "adaptive", "display": "omitted"}},
        id="thinking-adaptive-display-omitted",
    ),
    pytest.param(
        {
            **_base(),
            "thinking": {"type": "enabled", "budget_tokens": 8000},
        },
        id="thinking-enabled-with-budget",
    ),
    pytest.param(
        {**_base(), "thinking": {"type": "disabled"}},
        id="thinking-disabled",
    ),
    # --- cache_control TTL variants ---
    pytest.param(
        {
            **_base(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "x",
                            "cache_control": {"type": "ephemeral", "ttl": "5m"},
                        }
                    ],
                }
            ],
        },
        id="cache-ttl-5m",
    ),
    pytest.param(
        {
            **_base(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "x",
                            "cache_control": {"type": "ephemeral", "ttl": "1h"},
                        }
                    ],
                }
            ],
        },
        id="cache-ttl-1h",
    ),
    pytest.param(
        # Forward-compatibility: any future TTL string upstream defines.
        {
            **_base(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "x",
                            "cache_control": {"type": "ephemeral", "ttl": "24h"},
                        }
                    ],
                }
            ],
        },
        id="cache-ttl-future-value",
    ),
    # --- top-level fields added in 2025-2026 ---
    pytest.param(
        {**_base(), "service_tier": "standard_only"},
        id="top-level-service-tier",
    ),
    pytest.param(
        {**_base(), "service_tier": "auto"},
        id="top-level-service-tier-auto",
    ),
    pytest.param(
        {**_base(), "inference_geo": "us"},
        id="top-level-inference-geo",
    ),
    pytest.param(
        {**_base(), "container": "container_abc123"},
        id="top-level-container",
    ),
    # --- numeric fields where upstream may relax docs ---
    pytest.param(
        {**_base(), "temperature": 1.5},
        id="temperature-above-one",
    ),
    pytest.param(
        {**_base(), "top_p": 1.5},
        id="top-p-above-one",
    ),
    # --- image media_type forward-compat ---
    pytest.param(
        {
            **_base(),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/heic",
                                "data": "AAAA",
                            },
                        }
                    ],
                }
            ],
        },
        id="image-future-media-type",
    ),
    # --- tool_choice forward-compat ---
    pytest.param(
        {**_base(), "tool_choice": {"type": "future_mode"}},
        id="tool-choice-future-type",
    ),
    # --- web search server tool, 2026 version ---
    pytest.param(
        {
            **_base(),
            "tools": [
                {"type": "web_search_20260209", "name": "web_search"},
            ],
        },
        id="server-tool-web-search-2026",
    ),
    # --- combined: realistic Opus 4.7 coding agent shape ---
    pytest.param(
        {
            **_base(),
            "max_tokens": 64000,
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "xhigh"},
            "service_tier": "standard_only",
            "tool_choice": {"type": "auto"},
        },
        id="opus-4-7-agent-realistic",
    ),
]


@pytest.mark.parametrize("payload", ACCEPTED_PAYLOADS)
def test_route_does_not_422_upstream_valid_payloads(payload):
    """Pydantic must accept any shape upstream documents.

    The OAuth path is a transparent proxy. If this raises, the route returns
    422 before the body ever reaches Anthropic — the proxy starts lying about
    what it accepts.
    """
    MessagesAPIRequest.model_validate(payload)


# Invariants we *do* still want to enforce. These guard the parsed model
# itself; they're the floor below which the proxy can't function.
REJECTED_PAYLOADS = [
    pytest.param(
        {**_base(), "max_tokens": 0},
        id="max-tokens-zero",
    ),
    pytest.param(
        {**_base(), "max_tokens": -1},
        id="max-tokens-negative",
    ),
    pytest.param(
        {**_base(), "temperature": -0.1},
        id="temperature-negative",
    ),
    pytest.param(
        {**_base(), "messages": "not-a-list"},
        id="messages-wrong-type",
    ),
    pytest.param(
        {k: v for k, v in _base().items() if k != "messages"},
        id="missing-messages",
    ),
]


@pytest.mark.parametrize("payload", REJECTED_PAYLOADS)
def test_route_rejects_structurally_invalid_payloads(payload):
    """The model is permissive about *values*, not about *structure*."""
    with pytest.raises(ValidationError):
        MessagesAPIRequest.model_validate(payload)
