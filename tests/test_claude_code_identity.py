"""Claude Code wire identity: cch, billing block, derived ids, headers, version state.

The cch vectors (known 2.1.220 vectors and normalization vectors) are ported from
CLIProxyAPI (MIT, https://github.com/router-for-me/CLIProxyAPI),
internal/runtime/executor/claude_signing_test.go.
"""

import copy
import hashlib
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest

from app.utils import claude_code_identity as cci

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "claude_code_fingerprint.json").read_text()
)

DEVICE_ID = "ab" * 32
ACCOUNT_UUID = "1f0c6a9e-5c1b-4d3e-9a2f-7b8c9d0e1f2a"
CLIENT_V4 = "3F2504E0-4F89-41D3-9A0C-0305E82C3301"
NOT_V4 = str(uuid.uuid5(uuid.NAMESPACE_DNS, "example.com"))

# --------------------------------------------------------------------------
# cch — CLIProxyAPI vectors
# --------------------------------------------------------------------------

CCH_BASE = (
    '{"model":"model-a","messages":[{"role":"user","content":[{"type":"text","text":"x"}]}],'
    '"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.220.test; '
    'cc_entrypoint=sdk-cli; cch=00000;"},{"type":"text","text":"system-x"}],"tools":[],'
    '"metadata":{"user_id":"meta-x"},"max_tokens":1,"thinking":{"type":"adaptive",'
    '"display":"omitted"},"context_management":{"edits":[{"type":"clear_thinking_20251015",'
    '"keep":"all"}]},"output_config":{"effort":"high"},"stream":true}'
)
_META = '"metadata":{"user_id":"meta-x"}'
_THINKING = '"thinking":{"type":"adaptive","display":"omitted"}'
_CONTEXT = (
    '"context_management":{"edits":[{"type":"clear_thinking_20251015","keep":"all"}]}'
)


def _base_with(old: str, new: str) -> str:
    assert old in CCH_BASE
    return CCH_BASE.replace(old, new, 1)


CCH_KNOWN_VECTORS = [
    ("base", CCH_BASE, "7ee87"),
    (
        "model value ignored",
        _base_with('"model":"model-a"', '"model":"model-b"'),
        "7ee87",
    ),
    ("max tokens ignored", _base_with('"max_tokens":1', '"max_tokens":2'), "7ee87"),
    ("message changes hash", _base_with('"text":"x"', '"text":"y"'), "b9cc8"),
    ("system changes hash", _base_with('"system-x"', '"system-y"'), "a30d3"),
    (
        "metadata changes hash",
        _base_with('"user_id":"meta-x"', '"user_id":"meta-y"'),
        "7a89d",
    ),
    (
        "thinking changes hash",
        _base_with(_THINKING, '"thinking":{"type":"disabled"}'),
        "7205c",
    ),
    (
        "context changes hash",
        _base_with(_CONTEXT, '"context_management":{"edits":[]}'),
        "05073",
    ),
    ("effort changes hash", _base_with('"effort":"high"', '"effort":"low"'), "12366"),
    ("stream changes hash", _base_with('"stream":true', '"stream":false'), "60400"),
    (
        "tool changes hash",
        _base_with(
            '"tools":[]',
            '"tools":[{"name":"t","description":"d","input_schema":{"type":"object"}}]',
        ),
        "3d78d",
    ),
    (
        "extra field changes hash",
        _base_with('"stream":true}', '"stream":true,"extra_top":"extra"}'),
        "2d622",
    ),
    (
        "field order remains significant",
        '{"stream":true,"output_config":{"effort":"high"},'
        + _CONTEXT
        + ","
        + _THINKING
        + ","
        '"max_tokens":1,"metadata":{"user_id":"meta-x"},"tools":[],"system":[{"type":"text",'
        '"text":"x-anthropic-billing-header: cc_version=2.1.220.test; cc_entrypoint=sdk-cli; '
        'cch=00000;"},{"type":"text","text":"system-x"}],"messages":[{"role":"user","content":'
        '[{"type":"text","text":"x"}]}],"model":"model-a"}',
        "e5b6c",
    ),
    (
        "nested model value ignored",
        _base_with(_META, '"metadata":{"user_id":"meta-x","model":"a"}'),
        "0601b",
    ),
    (
        "nested max tokens member omitted",
        _base_with(_META, '"metadata":{"user_id":"meta-x","max_tokens":2}'),
        "7ee87",
    ),
    (
        "top level fallbacks member omitted",
        _base_with(
            '"stream":true}', '"stream":true,"fallbacks":[{"model":"fallback-a"}]}'
        ),
        "7ee87",
    ),
    (
        "nested fallbacks member omitted",
        _base_with(
            _META, '"metadata":{"user_id":"meta-x","fallbacks":[{"model":"nested-a"}]}'
        ),
        "7ee87",
    ),
    (
        "top level fallback credit token omitted",
        _base_with('"stream":true}', '"stream":true,"fallback_credit_token":"a"}'),
        "7ee87",
    ),
    (
        "nested fallback credit token omitted",
        _base_with(
            _META, '"metadata":{"user_id":"meta-x","fallback_credit_token":"a"}'
        ),
        "7ee87",
    ),
    (
        "trailing dispatch run keeps native comma",
        _base_with(
            _META,
            '"metadata":{"user_id":"meta-x","max_tokens":999,'
            '"fallbacks":[{"model":"fallback-model"}]}',
        ),
        "4589b",
    ),
    (
        "model before trailing dispatch run",
        _base_with(
            _META,
            '"metadata":{"user_id":"meta-x","model":"nested-model","max_tokens":999,'
            '"fallbacks":[{"model":"fallback-model"}],"fallback_credit_token":"not-a-real-token"}',
        ),
        "2d312",
    ),
    (
        "model splits dispatch runs",
        _base_with(
            _META,
            '"metadata":{"user_id":"meta-x","max_tokens":999,"model":"nested-model",'
            '"fallbacks":[{"model":"fallback-model"}]}',
        ),
        "0601b",
    ),
    (
        "ordinary nested member remains",
        _base_with(_META, '"metadata":{"user_id":"meta-x","plain":"a"}'),
        "8d74c",
    ),
    (
        "billing block only",
        '{"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.220.test; '
        'cc_entrypoint=sdk-cli; cch=00000;"}]}',
        "f2edb",
    ),
]


@pytest.mark.parametrize(
    "body, want",
    [(b, w) for _, b, w in CCH_KNOWN_VECTORS],
    ids=[n for n, _, _ in CCH_KNOWN_VECTORS],
)
def test_cch_matches_cliproxyapi_known_vectors(body, want):
    assert cci.compute_cch(body.encode()) == want


CCH_NORMALIZATION_VECTORS = [
    ('{"model":"claude","keep":1}', '{"model":"","keep":1}'),
    ('{"max_tokens":1,"keep":2}', '{"keep":2}'),
    ('{"keep":1,"fallbacks":[{"model":"x"}],"tail":2}', '{"keep":1,"tail":2}'),
    ('{"keep":1,"fallback_credit_token":"secret"}', '{"keep":1}'),
    ('{"max_tokens":1,"fallbacks":[],"fallback_credit_token":"secret"}', "{}"),
    ('{"keep":1,"max_tokens":1,"fallbacks":[],"tail":2}', '{"keep":1,"tail":2}'),
    ('{"keep":1,"max_tokens":1,"fallbacks":[]}', '{"keep":1,}'),
    (
        '{"outer":{"model":"x","max_tokens":1,"keep":"y"}}',
        '{"outer":{"model":"","keep":"y"}}',
    ),
    (
        '{"text":"literal \\"model\\":\\"x\\" and \\"max_tokens\\":1"}',
        '{"text":"literal \\"model\\":\\"x\\" and \\"max_tokens\\":1"}',
    ),
]


@pytest.mark.parametrize("body, want", CCH_NORMALIZATION_VECTORS)
def test_cch_hash_view_matches_cliproxyapi_normalization(body, want):
    scanner = cci._CCHScanner(body.encode())
    scanner.scan()
    assert scanner.normalized(body.encode()).decode() == want


def test_cch_zeroes_only_the_billing_digits_not_a_literal_in_a_message():
    literal = "keep literal cch=00000; in the message"
    unsigned = _base_with('"text":"x"', f'"text":"{literal}"').encode()
    cch = cci.compute_cch(unsigned)
    signed = unsigned.replace(b"sdk-cli; cch=00000;", f"sdk-cli; cch={cch};".encode())

    assert cci.compute_cch(signed) == cch
    assert json.loads(signed)["messages"][0]["content"][0]["text"] == literal


def test_cch_rejects_malformed_json():
    with pytest.raises(ValueError):
        cci.compute_cch(b'{"a":')


# --------------------------------------------------------------------------
# cc_version suffix and fingerprint text
# --------------------------------------------------------------------------


def test_version_suffix_known_vector():
    assert cci.version_suffix("Say hi", "2.1.283") == "a03"


def _expected_suffix(sampled: str, version: str) -> str:
    return hashlib.sha256(("59cf53e54c78" + sampled + version).encode()).hexdigest()[:3]


def test_version_suffix_indexes_utf16_code_units():
    # The emoji is two UTF-16 units, so index 4 is "c", 7 is "f", 20 is "s".
    text = "\U0001f600abcdefghijklmnopqrstuvwxyz"
    assert cci.version_suffix(text, "2.1.283") == _expected_suffix("cfs", "2.1.283")


def test_version_suffix_pads_missing_indices_and_replaces_lone_surrogates():
    assert cci.version_suffix("abc", "1.0.0") == _expected_suffix("000", "1.0.0")
    # Index 4 is the high half of an emoji pair: it hashes as U+FFFD.
    assert cci.version_suffix("abcd\U0001f600", "1.0.0") == _expected_suffix(
        "�00", "1.0.0"
    )


def test_fingerprint_text_skips_context_reminders_only():
    reminder = "<system-reminder>\nctx\n</system-reminder>"
    body = {
        "messages": [
            {"role": "assistant", "content": "ignored"},
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {}},
                    {"type": "text", "text": reminder},
                    {"type": "text", "text": "<system-reminder> unclosed"},
                    {"type": "text", "text": "later"},
                ],
            },
        ]
    }
    assert cci.fingerprint_message_text(body) == "<system-reminder> unclosed"
    assert (
        cci.fingerprint_message_text({"messages": [{"role": "user", "content": "hi"}]})
        == "hi"
    )
    assert cci.fingerprint_message_text({"messages": []}) == ""


def test_fingerprint_text_stops_at_the_first_user_message():
    reminder = {"type": "text", "text": "<system-reminder>x</system-reminder>"}
    body = {
        "messages": [
            {"role": "user", "content": [{"type": "image", "source": {}}, reminder]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "later"},
        ]
    }
    assert cci.fingerprint_message_text(body) == ""


# --------------------------------------------------------------------------
# Captured constants
# --------------------------------------------------------------------------


def test_constants_equal_the_captured_fixture():
    assert cci.FALLBACK_CLI_VERSION == FIXTURE["_cli_version"]
    assert cci.HEADER_ORDER == FIXTURE["header_order"]
    assert cci.STATIC_HEADERS == FIXTURE["static_headers"]
    assert cci.TLS_PROFILE == FIXTURE["tls"]
    assert cci.PLATFORM_HEADERS == FIXTURE["_platform_headers_captured"]
    assert cci.BILLING_PREFIX == FIXTURE["billing_block"]["prefix"]
    assert cci.IDENTITY_TEXT == FIXTURE["identity_block"]["text"]
    assert list(cci.BASE_BETAS) == FIXTURE["_betas_main"][:2]


# --------------------------------------------------------------------------
# Derived ids
# --------------------------------------------------------------------------


def _is_v4(value: str) -> bool:
    parsed = uuid.UUID(value)
    return (
        parsed.version == 4 and parsed.variant == uuid.RFC_4122 and str(parsed) == value
    )


def test_derived_ids_are_uuid4():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    session_id = cci.derive_session_id(DEVICE_ID, body)
    prompt_id = cci.derive_prompt_id(session_id, body)
    assert _is_v4(session_id)
    assert _is_v4(prompt_id)
    assert session_id != prompt_id

    finalized = cci.finalize_messages_body(body, device_id=DEVICE_ID, account_uuid="")
    assert _is_v4(finalized.session_id)
    assert _is_v4(finalized.prompt_id)


def test_session_id_depends_on_device():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    assert cci.derive_session_id("a" * 64, body) != cci.derive_session_id(
        "b" * 64, body
    )


# --------------------------------------------------------------------------
# finalize_messages_body
# --------------------------------------------------------------------------


def _finalize(body: dict, **kwargs) -> tuple[dict, cci.FinalizedBody]:
    kwargs.setdefault("device_id", DEVICE_ID)
    kwargs.setdefault("account_uuid", ACCOUNT_UUID)
    finalized = cci.finalize_messages_body(body, **kwargs)
    return json.loads(finalized.body_bytes), finalized


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _billing_pattern(prompt_id: str, prev_req: str = "") -> re.Pattern:
    prev = f" cc_prev_req={re.escape(prev_req)};" if prev_req else ""
    return re.compile(
        rf"x-anthropic-billing-header: cc_version={re.escape(cci.cli_version())}\.[0-9a-f]{{3}}; "
        rf"cc_entrypoint=cli; cch=[0-9a-f]{{5}};{prev} cc_prompt_id={prompt_id}; "
        r"cc_turn_origin=human;"
    )


def test_finalize_writes_the_captured_billing_and_identity_layout():
    out, finalized = _finalize({"model": "m", "messages": [_user("Say hi")]})

    billing, identity = out["system"]
    assert billing == {"type": "text", "text": billing["text"]}  # no cache_control
    assert _billing_pattern(finalized.prompt_id).fullmatch(billing["text"])
    assert f"cc_version={cci.cli_version()}.a03;" in billing["text"]
    fields = re.findall(r"(\w+)=", billing["text"].removeprefix(cci.BILLING_PREFIX))
    assert fields == FIXTURE["billing_block"]["fields"]
    assert identity == {"type": "text", "text": cci.IDENTITY_TEXT}
    assert finalized.cli_version == cci.cli_version()
    assert cci.verify_billing(finalized.body_bytes) == (True, True)


def test_finalize_output_is_compact_json():
    _, finalized = _finalize(
        {"model": "m", "messages": [_user("é ✓")], "max_tokens": 5}
    )
    reserialized = json.dumps(
        json.loads(finalized.body_bytes), separators=(",", ":"), ensure_ascii=False
    )
    assert finalized.body_bytes == reserialized.encode()


def test_finalize_preserves_unknown_fields():
    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "future_block_field": {"a": [1, None]},
                    }
                ],
            }
        ],
        "tools": [
            {"name": "t", "input_schema": {"type": "object"}, "future_tool_field": True}
        ],
        "future_top_level": {"nested": ["x", 1.5, False]},
        "stream": True,
    }
    out, _ = _finalize(body)
    for key in ("messages", "tools", "future_top_level", "stream", "model"):
        assert out[key] == body[key]


def test_finalize_replaces_client_billing_and_keeps_client_identity_in_place():
    client_identity = {
        "type": "text",
        "text": cci.IDENTITY_TEXT,
        "cache_control": {"type": "ephemeral"},
    }
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "system": [
            {
                "type": "text",
                "text": "x-anthropic-billing-header: cc_version=1.0.0.abc; cch=12345;",
                "cache_control": {"type": "ephemeral"},
            },
            client_identity,
            {"type": "text", "text": "rest", "cache_control": {"type": "ephemeral"}},
        ],
    }
    out, finalized = _finalize(body)

    assert len(out["system"]) == 3
    assert _billing_pattern(finalized.prompt_id).fullmatch(out["system"][0]["text"])
    assert out["system"][1] == client_identity
    assert out["system"][2] == body["system"][2]


def test_finalize_adds_identity_when_the_client_identity_is_not_first():
    system = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": cci.IDENTITY_TEXT},
    ]
    out, _ = _finalize({"model": "m", "messages": [_user("hi")], "system": system})
    assert out["system"][1:] == [{"type": "text", "text": cci.IDENTITY_TEXT}, *system]


def test_finalize_rejects_a_system_of_another_type():
    with pytest.raises(ValueError):
        _finalize({"model": "m", "messages": [_user("hi")], "system": {"text": "x"}})


def test_finalize_wraps_a_string_system():
    out, _ = _finalize({"model": "m", "messages": [_user("hi")], "system": "be terse"})
    assert out["system"][1:] == [
        {"type": "text", "text": cci.IDENTITY_TEXT},
        {"type": "text", "text": "be terse"},
    ]


@pytest.mark.parametrize("system", ["", [], None])
def test_finalize_treats_empty_system_as_missing(system):
    out, _ = _finalize({"model": "m", "messages": [_user("hi")], "system": system})
    assert len(out["system"]) == 2
    assert out["system"][1]["text"] == cci.IDENTITY_TEXT


def test_finalize_without_identity_omits_only_our_identity_block():
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "system": [{"type": "text", "text": "x"}],
    }
    out, finalized = _finalize(body, include_identity=False)
    assert out["system"][1:] == [{"type": "text", "text": "x"}]
    assert _billing_pattern(finalized.prompt_id).fullmatch(out["system"][0]["text"])

    body["system"].insert(0, {"type": "text", "text": cci.IDENTITY_TEXT})
    out, _ = _finalize(body, include_identity=False)
    assert out["system"][1:] == body["system"]


def test_finalize_user_id_is_a_json_string_in_cli_key_order():
    out, finalized = _finalize({"model": "m", "messages": [_user("hi")]})
    user_id = out["metadata"]["user_id"]
    assert isinstance(user_id, str)
    assert json.loads(user_id) == {
        "device_id": DEVICE_ID,
        "account_uuid": ACCOUNT_UUID,
        "session_id": finalized.session_id,
    }
    assert list(json.loads(user_id)) == FIXTURE["user_id_keys"]


def test_finalize_inserts_system_and_metadata_at_cli_positions():
    with_tools = {"model": "m", "messages": [_user("hi")], "tools": [], "max_tokens": 1}
    out, _ = _finalize(with_tools)
    assert list(out) == [
        "model",
        "messages",
        "system",
        "tools",
        "metadata",
        "max_tokens",
    ]

    without_tools = {
        "max_tokens": 1,
        "messages": [_user("hi")],
        "model": "m",
        "stream": True,
    }
    out, _ = _finalize(without_tools)
    assert list(out) == [
        "max_tokens",
        "messages",
        "system",
        "metadata",
        "model",
        "stream",
    ]


def test_finalize_keeps_client_key_order_for_existing_keys():
    body = {
        "stream": True,
        "metadata": {"other": 1},
        "system": "s",
        "model": "m",
        "messages": [_user("hi")],
    }
    out, _ = _finalize(body)
    assert list(out) == list(body)
    assert list(out["metadata"]) == ["other", "user_id"]


def test_finalize_does_not_mutate_its_input():
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "system": [
            {"type": "text", "text": "x-anthropic-billing-header: old;"},
            {"type": "text", "text": "rest"},
        ],
        "metadata": {"user_id": json.dumps({"session_id": CLIENT_V4, "extra": 1})},
        "tools": [{"name": "t"}],
    }
    before = copy.deepcopy(body)
    _finalize(body, client_prompt_id=CLIENT_V4, prev_request_id="req_abc")
    assert body == before


def test_finalize_escapes_lone_surrogates():
    _, finalized = _finalize({"model": "m", "messages": [_user("a\ud800b")]})
    assert b'"a\\ud800b"' in finalized.body_bytes
    assert cci.verify_billing(finalized.body_bytes)[0]


def test_finalize_rejects_nan():
    with pytest.raises(ValueError):
        _finalize(
            {"model": "m", "messages": [_user("hi")], "temperature": float("nan")}
        )


def test_prompt_id_holds_across_a_tool_loop_and_changes_per_human_turn():
    reminder = {"type": "text", "text": "<system-reminder>x</system-reminder>"}
    turn_1 = [_user("list files")]
    tool_loop = turn_1 + [
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "ls", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1"}, reminder],
        },
    ]
    turn_2 = tool_loop + [
        {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        _user("thanks"),
    ]
    ids = [
        _finalize({"model": "m", "messages": m})[1] for m in (turn_1, tool_loop, turn_2)
    ]

    assert ids[0].prompt_id == ids[1].prompt_id != ids[2].prompt_id
    assert ids[0].session_id == ids[1].session_id == ids[2].session_id


def test_session_id_ignores_cache_control_moving_between_turns():
    cached = {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    turn_1 = [{"role": "user", "content": [cached]}]
    turn_2 = [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": [dict(cached, text="more")]},
    ]
    assert cci.derive_session_id(
        DEVICE_ID, {"messages": turn_1}
    ) == cci.derive_session_id(DEVICE_ID, {"messages": turn_2})


def test_session_id_treats_string_content_as_one_text_block():
    as_string = {"messages": [{"role": "user", "content": "hi"}]}
    as_block = {"messages": [_user("hi")]}
    assert cci.derive_session_id(DEVICE_ID, as_string) == cci.derive_session_id(
        DEVICE_ID, as_block
    )
    assert as_string["messages"][0]["content"] == "hi"  # input untouched


def test_finalize_adopts_client_v4_session_and_prompt_ids():
    client_user_id = json.dumps(
        {
            "device_id": "theirs",
            "account_uuid": "theirs",
            "session_id": CLIENT_V4,
            "extra": 7,
        }
    )
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "metadata": {"user_id": client_user_id},
    }
    out, finalized = _finalize(body, client_prompt_id=CLIENT_V4.upper())

    assert finalized.session_id == CLIENT_V4.lower()
    assert finalized.prompt_id == CLIENT_V4.lower()
    user_id = json.loads(out["metadata"]["user_id"])
    assert list(user_id) == ["device_id", "account_uuid", "session_id", "extra"]
    assert user_id["device_id"] == DEVICE_ID
    assert user_id["account_uuid"] == ACCOUNT_UUID
    assert user_id["session_id"] == CLIENT_V4.lower()
    assert f"cc_prompt_id={CLIENT_V4.lower()};" in out["system"][0]["text"]


@pytest.mark.parametrize("bad", [NOT_V4, "not-a-uuid", str(uuid.uuid1())])
def test_finalize_ignores_non_v4_client_ids(bad):
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "metadata": {"user_id": json.dumps({"session_id": bad, "extra": 1})},
    }
    out, finalized = _finalize(body, client_prompt_id=bad)
    derived_session = cci.derive_session_id(DEVICE_ID, body)
    assert finalized.session_id == derived_session
    assert finalized.prompt_id == cci.derive_prompt_id(derived_session, body)
    assert list(json.loads(out["metadata"]["user_id"])) == FIXTURE["user_id_keys"]


@pytest.mark.parametrize(
    "client_user_id",
    [
        "user_abc",
        '{"session_id":"' + CLIENT_V4 + '","x":NaN}',
        "[" * 100_000 + "]" * 100_000,
    ],
    ids=["not-json", "nan", "deeply-nested"],
)
def test_finalize_ignores_an_unusable_client_user_id(client_user_id):
    body = {
        "model": "m",
        "messages": [_user("hi")],
        "metadata": {"user_id": client_user_id},
    }
    out, finalized = _finalize(body)
    assert list(json.loads(out["metadata"]["user_id"])) == FIXTURE["user_id_keys"]
    assert finalized.session_id == cci.derive_session_id(DEVICE_ID, body)


def test_finalize_adds_cc_prev_req_between_cch_and_prompt_id():
    out, finalized = _finalize(
        {"model": "m", "messages": [_user("hi")]},
        prev_request_id="req_a12486cc13dbefa2c38635f3",
    )
    billing = out["system"][0]["text"]
    assert _billing_pattern(
        finalized.prompt_id, "req_a12486cc13dbefa2c38635f3"
    ).fullmatch(billing)
    assert cci.verify_billing(finalized.body_bytes) == (True, True)


def test_finalize_without_prev_request_has_no_cc_prev_req():
    out, _ = _finalize({"model": "m", "messages": [_user("hi")]}, prev_request_id=None)
    assert "cc_prev_req" not in out["system"][0]["text"]


def test_finalize_drops_a_malformed_prev_request_id():
    out, finalized = _finalize(
        {"model": "m", "messages": [_user("hi")]}, prev_request_id="req_x; cc_evil=1"
    )
    assert _billing_pattern(finalized.prompt_id).fullmatch(out["system"][0]["text"])


def test_verify_billing_flags_a_tampered_body():
    _, finalized = _finalize({"model": "m", "messages": [_user("Say hi")]})
    tampered = finalized.body_bytes.replace(b"Say hi", b"Say ho")
    assert cci.verify_billing(tampered) == (False, True)
    wrong_suffix = re.sub(
        rb"cc_version=([0-9.]+)\.a03;", rb"cc_version=\1.fff;", finalized.body_bytes
    )
    assert cci.verify_billing(wrong_suffix)[1] is False
    assert cci.verify_billing(b'{"model":"m","messages":[]}') == (False, False)


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------

_TRANSPORT_HEADERS = ("Host", "Content-Length")


def test_messages_headers_follow_the_captured_order_and_values():
    headers = cci.build_messages_headers(
        "tok",
        client_beta_header=None,
        session_id="sess",
        prompt_id="prompt",
        cli_version="2.1.283",
    )
    names = [name for name, _ in headers]
    assert names == [h for h in FIXTURE["header_order"] if h not in _TRANSPORT_HEADERS]

    values = dict(headers)
    for name, value in FIXTURE["static_headers"].items():
        assert values[name] == value
    for name, value in FIXTURE["_platform_headers_captured"].items():
        assert values[name] == value
    assert values["User-Agent"] == FIXTURE["user_agent_template"].format(
        version="2.1.283"
    )
    assert values["Authorization"] == "Bearer tok"
    assert values["X-Claude-Code-Session-Id"] == "sess"
    assert values["x-claude-code-prompt-id"] == "prompt"
    assert values["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
    assert _is_v4(values["x-client-request-id"])


def test_messages_headers_take_the_request_class():
    headers = cci.build_messages_headers(
        "tok",
        client_beta_header=None,
        session_id="s",
        prompt_id="p",
        cli_version="2.1.283",
        request_class="background",
    )
    assert dict(headers)["x-claude-code-request-class"] == "background"


def test_betas_are_merged_after_the_base_set_and_deduped():
    headers = cci.build_api_headers(
        "tok",
        client_beta_header=" oauth-2025-04-20, foo ,claude-code-20250219,foo,,bar",
        cli_version="2.1.283",
    )
    assert dict(headers)["anthropic-beta"] == (
        "claude-code-20250219,oauth-2025-04-20,foo,bar"
    )


def test_request_id_is_fresh_per_call():
    first = dict(cci.build_api_headers("tok", cli_version="2.1.283"))
    second = dict(cci.build_api_headers("tok", cli_version="2.1.283"))
    assert first["x-client-request-id"] != second["x-client-request-id"]


def test_api_headers_drop_messages_only_headers():
    headers = cci.build_api_headers("tok", cli_version="9.9.9")
    dropped = {
        "Content-Type",
        "X-Claude-Code-Session-Id",
        "x-claude-code-prompt-id",
        "x-claude-code-request-class",
        *_TRANSPORT_HEADERS,
    }
    assert [name for name, _ in headers] == [
        h for h in cci.HEADER_ORDER if h not in dropped
    ]
    assert dict(headers)["User-Agent"] == "claude-cli/9.9.9 (external, cli)"


# --------------------------------------------------------------------------
# Version state
# --------------------------------------------------------------------------


def _fresh_version_state(monkeypatch):
    monkeypatch.setattr(cci, "_version", cci.FALLBACK_CLI_VERSION)
    monkeypatch.setattr(cci, "_pinned", False)


def _fake_npm(monkeypatch, handler):
    """Route refresh_cli_version's httpx client through ``handler``; return seen URLs."""
    seen = []
    real_client = httpx.AsyncClient

    def recording_handler(request):
        seen.append(str(request.url))
        return handler(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(recording_handler), **kwargs
        ),
    )
    return seen


def test_bump_is_upward_only_and_numeric(monkeypatch):
    _fresh_version_state(monkeypatch)
    assert cci.bump_cli_version("2.1.1000") is True
    assert cci.cli_version() == "2.1.1000"
    assert cci.bump_cli_version("2.1.1000") is False
    assert cci.bump_cli_version("2.1.999") is False
    assert cci.bump_cli_version("3.0.0") is True
    assert cci.cli_version() == "3.0.0"


@pytest.mark.parametrize(
    "garbage", ["", "2.1", "v2.1.300", "2.1.300-beta", "2.1.300\n", "٣.1.1"]
)
def test_bump_rejects_malformed_versions(monkeypatch, garbage):
    _fresh_version_state(monkeypatch)
    assert cci.bump_cli_version(garbage) is False
    assert cci.cli_version() == cci.FALLBACK_CLI_VERSION


async def test_pin_blocks_refresh_and_bump(monkeypatch):
    _fresh_version_state(monkeypatch)
    seen = _fake_npm(
        monkeypatch, lambda request: httpx.Response(200, json={"version": "9.9.9"})
    )

    cci.pin_cli_version("2.1.100")
    assert cci.bump_cli_version("9.9.9") is False
    await cci.refresh_cli_version()
    assert cci.cli_version() == "2.1.100"
    assert seen == []

    cci.pin_cli_version(None)
    await cci.refresh_cli_version()
    assert cci.cli_version() == "9.9.9"
    assert seen == [cci.NPM_LATEST_URL]


def test_unpin_never_leaves_the_version_below_the_fallback(monkeypatch):
    _fresh_version_state(monkeypatch)
    cci.pin_cli_version("1.0.0")
    cci.pin_cli_version(None)
    assert cci.cli_version() == cci.FALLBACK_CLI_VERSION

    cci.pin_cli_version("9.9.9")
    cci.pin_cli_version(None)
    assert cci.cli_version() == "9.9.9"


def test_pin_rejects_malformed_versions(monkeypatch):
    _fresh_version_state(monkeypatch)
    with pytest.raises(ValueError):
        cci.pin_cli_version("latest")
    assert cci.cli_version() == cci.FALLBACK_CLI_VERSION


def _connect_error(request):
    raise httpx.ConnectError("registry unreachable", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503, text="unavailable"),
        lambda request: httpx.Response(200, content=b"<html>not json"),
        lambda request: httpx.Response(200, json=["2.1.300"]),
        lambda request: httpx.Response(200, json={"version": "2.1"}),
        lambda request: httpx.Response(200, json={"version": None}),
        lambda request: httpx.Response(200, json={"version": "2.1.1"}),
        _connect_error,
    ],
    ids=[
        "http-503",
        "bad-json",
        "not-an-object",
        "bad-version",
        "no-version",
        "older",
        "down",
    ],
)
async def test_refresh_keeps_the_last_good_version(monkeypatch, handler):
    _fresh_version_state(monkeypatch)
    cci.bump_cli_version("2.1.290")
    _fake_npm(monkeypatch, handler)
    await cci.refresh_cli_version()
    assert cci.cli_version() == "2.1.290"
