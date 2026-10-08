"""Claude Code CLI wire identity for OAuth calls to the Anthropic API.

Pure functions plus the module-level CLI version: body finalization (billing
block, identity block, ``metadata.user_id``, ``cch``) and the ordered header
sets the genuine CLI sends. Ground truth is Claude Code 2.1.283, captured by
``scripts/capture_claude_code.py``; the constants below are the runtime source
of truth and a test pins them to ``tests/fixtures/claude_code_fingerprint.json``,
which is never read at runtime.

Transport contract: the header builders return their headers in
``HEADER_ORDER`` order and casing but never ``Host`` or ``Content-Length``;
the transport computes those and places them at their ``HEADER_ORDER``
position. ``Connection`` and ``Accept-Encoding`` are included because the
transport must send them exactly as the CLI does rather than supply its own.

Serialization matches ``JSON.stringify`` for strings (lone surrogates become
``\\udXXX`` escapes) but not for floats: Python prints ``1e5`` as ``100000.0``
and may round-trip large floats differently, which upstream accepts.

The cch scanner, its test vectors and the billing-text layout are ported from
CLIProxyAPI (MIT, https://github.com/router-for-me/CLIProxyAPI),
``internal/runtime/executor/claude_signing.go`` and
``claude_executor_cloaking.go``.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

import httpx
import xxhash
from loguru import logger

# --------------------------------------------------------------------------
# Captured constants (Claude Code 2.1.283)
# --------------------------------------------------------------------------

FALLBACK_CLI_VERSION = "2.1.283"

HEADER_ORDER = [
    "Accept",
    "Authorization",
    "Content-Type",
    "User-Agent",
    "X-Claude-Code-Session-Id",
    "X-Stainless-Arch",
    "X-Stainless-Lang",
    "X-Stainless-OS",
    "X-Stainless-Package-Version",
    "X-Stainless-Retry-Count",
    "X-Stainless-Runtime",
    "X-Stainless-Runtime-Version",
    "X-Stainless-Timeout",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "anthropic-version",
    "x-app",
    "x-claude-code-prompt-id",
    "x-claude-code-request-class",
    "x-client-request-id",
    "Connection",
    "Host",
    "Accept-Encoding",
    "Content-Length",
]

STATIC_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "X-Stainless-Lang": "js",
    "X-Stainless-Package-Version": "0.112.1",
    "X-Stainless-Retry-Count": "0",
    "X-Stainless-Runtime": "node",
    "X-Stainless-Runtime-Version": "v26.3.0",
    "X-Stainless-Timeout": "600",
    "anthropic-dangerous-direct-browser-access": "true",
    "anthropic-version": "2023-06-01",
    "x-app": "cli",
    "x-claude-code-request-class": "main",
    "Connection": "keep-alive",
    "Accept-Encoding": "gzip, deflate, br, zstd",
}

# The platform the TLS fingerprint was captured on.
PLATFORM_HEADERS = {"X-Stainless-OS": "Linux", "X-Stainless-Arch": "x64"}

TLS_PROFILE = {
    "ciphers": [
        "0x1301",
        "0x1302",
        "0x1303",
        "0xc02b",
        "0xc02f",
        "0xc02c",
        "0xc030",
        "0xcca9",
        "0xcca8",
        "0xc009",
        "0xc013",
        "0xc00a",
        "0xc014",
        "0x009c",
        "0x009d",
        "0x002f",
        "0x0035",
    ],
    "compression_methods_hex": "0100",
    "extension_order": [0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43],
    "_conditional_extensions_note": (
        "21 (padding) and 41 (pre_shared_key) may appear depending on hello "
        "length / resumption; excluded from extension_order"
    ),
    "alpn": ["http/1.1"],
    "grease": False,
    "supported_groups_ext_hex": "000811ec001d00170018",
    "signature_algorithms_ext_hex": "0012040308040401050308050501080606010201",
    "supported_versions_ext_hex": "0403040303",
    "ec_point_formats_ext_hex": "0100",
    "psk_modes_ext_hex": "0101",
    "key_share_groups": ["0x11ec", "0x001d"],
}

BILLING_PREFIX = "x-anthropic-billing-header: "
IDENTITY_TEXT = "You are Claude Code, Anthropic's official CLI for Claude."
BASE_BETAS = ("claude-code-20250219", "oauth-2025-04-20")

_USER_AGENT_TEMPLATE = "claude-cli/{version} (external, cli)"
# Client billing blocks are recognised without the trailing space, as CLIProxyAPI does.
_BILLING_MARKER = BILLING_PREFIX.rstrip()
_SUFFIX_SALT = "59cf53e54c78"
_SUFFIX_INDICES = (4, 7, 20)

_CCH_SEED = 0x4D659218E32A3268
_CCH_PLACEHOLDER = "00000"
_CCH_DIGITS = re.compile(rb"[0-9a-f]{5}")

_UUID4 = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9_-]+")
_SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_BILLING_VERSION = re.compile(r"cc_version=([0-9]+\.[0-9]+\.[0-9]+)\.([0-9a-f]{3});")
# Upstream's per-model CLI version gate: "Claude Code X does not support this
# model; version Y or newer is required".
_VERSION_GATE = re.compile(r"version (\d+\.\d+\.\d+) or newer is required")

# --------------------------------------------------------------------------
# CLI version state
# --------------------------------------------------------------------------

NPM_LATEST_URL = "https://registry.npmjs.org/@anthropic-ai/claude-code/latest"
_NPM_TIMEOUT_SECONDS = 10.0

_version = FALLBACK_CLI_VERSION
_pinned = False


def _semver(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or not _SEMVER.fullmatch(value):
        return None
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def cli_version() -> str:
    """The Claude Code version Clove currently presents."""
    return _version


def pin_cli_version(version: str | None) -> None:
    """Pin the presented version (``CLAUDE_CODE_VERSION``); ``None`` unpins.

    While pinned, ``refresh_cli_version`` and ``bump_cli_version`` do nothing.
    Unpinning never leaves the version below ``FALLBACK_CLI_VERSION``.
    Raises ``ValueError`` for anything other than ``MAJOR.MINOR.PATCH``.
    """
    global _version, _pinned
    if version is None:
        _pinned = False
        bump_cli_version(FALLBACK_CLI_VERSION)
        return
    if _semver(version) is None:
        raise ValueError(
            f"Claude Code version must be MAJOR.MINOR.PATCH, got {version!r}"
        )
    _version = version
    _pinned = True


def bump_cli_version(min_version: str) -> bool:
    """Raise the presented version to ``min_version`` if that is newer.

    Upward only and ignored while pinned or when ``min_version`` is not
    ``MAJOR.MINOR.PATCH``. Returns whether this call changed the version. A
    concurrent request may already have bumped it, so a caller deciding whether
    to retry should also compare its request's version with ``cli_version()``.
    """
    global _version
    if _pinned:
        return False
    wanted = _semver(min_version)
    if wanted is None:
        logger.warning(f"Ignoring malformed Claude Code version {min_version!r}")
        return False
    if wanted <= _semver(_version):
        return False
    logger.info(f"Claude Code version raised from {_version} to {min_version}")
    _version = min_version
    return True


def parse_version_gate(message: str) -> str | None:
    """The version an upstream version-gate error demands (``Y`` in
    ``... version Y or newer is required``), else ``None``."""
    match = _VERSION_GATE.search(message)
    return match.group(1) if match else None


async def refresh_cli_version() -> None:
    """Adopt npm's latest Claude Code version if it is newer; never raises.

    Any failure (network, HTTP status, JSON, malformed version) is logged and
    the current value is kept. The fetch deliberately bypasses Clove's proxy
    settings; an ``HTTP(S)_PROXY`` from the environment still applies.
    """
    if _pinned:
        return
    try:
        async with httpx.AsyncClient(timeout=_NPM_TIMEOUT_SECONDS) as client:
            response = await client.get(NPM_LATEST_URL)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.warning(
            f"Claude Code version refresh failed, keeping {_version}: {exc!r}"
        )
        return
    latest = data.get("version") if isinstance(data, dict) else None
    if _semver(latest) is None:
        logger.warning(
            f"npm returned no usable Claude Code version ({latest!r}), keeping {_version}"
        )
        return
    bump_cli_version(latest)


# --------------------------------------------------------------------------
# Serialization and cch
# --------------------------------------------------------------------------


def serialize(body: dict) -> bytes:
    """Compact JSON bytes as the CLI sends them.

    Lone surrogates are escaped as ``\\udXXX`` the way ``JSON.stringify`` does
    (they cannot be encoded as UTF-8). NaN and infinities raise ``ValueError``.
    """
    text = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # only lone surrogates fail to encode
        return _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text).encode(
            "utf-8"
        )


_WHITESPACE_BYTES = b" \t\r\n"
_WHITESPACE = re.compile(rb"[ \t\r\n]*")
_STRING = re.compile(rb'"[^"\\]*+(?:\\.[^"\\]*+)*+"', re.DOTALL)
_SCALAR = re.compile(rb"[^,}\] \t\r\n]*")
_MODEL_KEY = b'"model"'
_SYSTEM_KEY = b'"system"'
_TEXT_KEY = b'"text"'
_EXCLUDED_KEYS = frozenset(
    {b'"max_tokens"', b'"fallbacks"', b'"fallback_credit_token"'}
)

# Where a value sits relative to the billing text ``system[0].text``.
_TRACK_NONE, _TRACK_ROOT, _TRACK_SYSTEM, _TRACK_SYSTEM_FIRST, _TRACK_BILLING = range(5)


@dataclass
class _Member:
    start: int
    end: int
    comma_before: int
    comma_after: int
    excluded: bool


class _CCHScanner:
    """Byte-level port of CLIProxyAPI's ``normalizeClaudeCCHInput``.

    Records the edits that form the CLI's hash view (``model`` string values
    emptied; ``max_tokens`` / ``fallbacks`` / ``fallback_credit_token`` members
    dropped at any depth, keeping the CLI's stray comma before a trailing run of
    two or more) and, in the same pass, the raw span of ``system[0].text``.
    """

    def __init__(self, body: bytes):
        self.body = body
        self.pos = 0
        self.edits: list[tuple[int, int]] = []
        self.billing_span: tuple[int, int] | None = None

    def scan(self) -> None:
        try:
            self._value(True, _TRACK_ROOT)
        except RecursionError:
            raise ValueError("JSON body nested too deeply") from None
        self._skip_whitespace()
        if self.pos != len(self.body):
            raise ValueError(f"unexpected JSON data at byte {self.pos}")

    def normalized(self, body: bytes) -> bytes:
        """``body`` (same length as the scanned bytes) with the edits applied."""
        parts = []
        last = 0
        for start, end in sorted(self.edits):
            if start < last:
                raise ValueError(f"overlapping cch normalization edit at byte {start}")
            parts.append(body[last:start])
            last = end
        parts.append(body[last:])
        return b"".join(parts)

    def _value(self, collect: bool, track: int) -> None:
        self._skip_whitespace()
        if self.pos >= len(self.body):
            raise ValueError(f"missing JSON value at byte {self.pos}")
        char = self.body[self.pos]
        if char == ord("{"):
            self._object(collect, track)
        elif char == ord("["):
            self._array(collect, track)
        elif char == ord('"'):
            span = self._string()
            if track == _TRACK_BILLING and self.billing_span is None:
                self.billing_span = span
        else:
            end = _SCALAR.match(self.body, self.pos).end()
            if end == self.pos:
                raise ValueError(f"missing JSON value at byte {self.pos}")
            self.pos = end

    def _object(self, collect: bool, track: int) -> None:
        self.pos += 1
        self._skip_whitespace()
        if self._consume(ord("}")):
            return
        members: list[_Member] = []
        comma_before = -1
        while True:
            self._skip_whitespace()
            member_start = self.pos
            key_start, key_end = self._string()
            self._skip_whitespace()
            if not self._consume(ord(":")):
                raise ValueError(f"missing object colon at byte {self.pos}")
            self._skip_whitespace()

            key = self.body[key_start:key_end]
            excluded = collect and key in _EXCLUDED_KEYS
            if (
                collect
                and key == _MODEL_KEY
                and self.pos < len(self.body)
                and self.body[self.pos] == ord('"')
            ):
                value_start, value_end = self._string()
                self._edit(value_start + 1, value_end - 1)
            else:
                if track == _TRACK_ROOT and key == _SYSTEM_KEY:
                    child = _TRACK_SYSTEM
                elif track == _TRACK_SYSTEM_FIRST and key == _TEXT_KEY:
                    child = _TRACK_BILLING
                else:
                    child = _TRACK_NONE
                self._value(collect and not excluded, child)
            member_end = self.pos
            self._skip_whitespace()

            comma_after = -1
            if self._consume(ord(",")):
                comma_after = self.pos - 1
            members.append(
                _Member(member_start, member_end, comma_before, comma_after, excluded)
            )
            if comma_after >= 0:
                comma_before = comma_after
                continue
            if not self._consume(ord("}")):
                raise ValueError(f"missing object end at byte {self.pos}")
            break
        if collect:
            self._excluded_member_edits(members)

    def _array(self, collect: bool, track: int) -> None:
        self.pos += 1
        self._skip_whitespace()
        if self._consume(ord("]")):
            return
        child = _TRACK_SYSTEM_FIRST if track == _TRACK_SYSTEM else _TRACK_NONE
        while True:
            self._value(collect, child)
            child = _TRACK_NONE
            self._skip_whitespace()
            if self._consume(ord(",")):
                continue
            if not self._consume(ord("]")):
                raise ValueError(f"missing array end at byte {self.pos}")
            return

    def _string(self) -> tuple[int, int]:
        match = _STRING.match(self.body, self.pos)
        if match is None:
            raise ValueError(f"missing or unterminated JSON string at byte {self.pos}")
        self.pos = match.end()
        return match.start(), match.end()

    def _excluded_member_edits(self, members: list[_Member]) -> None:
        start = 0
        while start < len(members):
            if not members[start].excluded:
                start += 1
                continue
            end = start
            while end + 1 < len(members) and members[end + 1].excluded:
                end += 1
            if end + 1 < len(members):
                self._edit(members[start].start, members[end].comma_after + 1)
            elif start > 0 and end > start:
                # Claude Code leaves the preceding comma in its hash view when an
                # object ends with two or more consecutive dispatch members.
                self._edit(members[start].start, members[end].end)
            elif start > 0:
                self._edit(members[start].comma_before, members[end].end)
            else:
                self._edit(members[start].start, members[end].end)
            start = end + 1

    def _edit(self, start: int, end: int) -> None:
        if start < end:
            self.edits.append((start, end))

    def _skip_whitespace(self) -> None:
        # Compact bodies have no whitespace; skip the regex call in that case.
        if self.pos < len(self.body) and self.body[self.pos] in _WHITESPACE_BYTES:
            self.pos = _WHITESPACE.match(self.body, self.pos).end()

    def _consume(self, char: int) -> bool:
        if self.pos < len(self.body) and self.body[self.pos] == char:
            self.pos += 1
            return True
        return False


def _cch_digits_offset(body: bytes, span: tuple[int, int] | None) -> int | None:
    """Offset of the 5 cch digits inside the billing block, as CLIProxyAPI finds them."""
    if span is None:
        return None
    start, end = span
    raw = body[start:end]
    if not json.loads(raw).startswith(_BILLING_MARKER):
        return None
    search = 0
    while (found := raw.find(b"cch=", search)) >= 0:
        digits = found + len(b"cch=")
        stop = digits + len(_CCH_PLACEHOLDER)
        if (
            stop < len(raw)
            and raw[stop] == ord(";")
            and _CCH_DIGITS.fullmatch(raw[digits:stop])
        ):
            return start + digits
        search = digits
    return None


def _sign(body_bytes: bytes) -> tuple[int | None, str]:
    """(billing cch offset or None, cch the CLI would send for these bytes)."""
    scanner = _CCHScanner(body_bytes)
    scanner.scan()
    offset = _cch_digits_offset(body_bytes, scanner.billing_span)
    unsigned = body_bytes
    if offset is not None:
        placeholder = _CCH_PLACEHOLDER.encode()
        unsigned = (
            body_bytes[:offset] + placeholder + body_bytes[offset + len(placeholder) :]
        )
    digest = xxhash.xxh64_intdigest(scanner.normalized(unsigned), seed=_CCH_SEED)
    return offset, f"{digest & 0xFFFFF:05x}"


def compute_cch(body_bytes: bytes) -> str:
    """The ``cch`` Claude Code computes for a serialized body.

    The billing block's own cch digits, if present, are zeroed first, so this
    works on both unsigned (``cch=00000``) and signed bytes. Raises
    ``ValueError`` on malformed JSON structure.
    """
    return _sign(body_bytes)[1]


# --------------------------------------------------------------------------
# Derived ids and the cc_version suffix
# --------------------------------------------------------------------------


def _uuid4_from(domain: str, *parts: str) -> str:
    """Deterministic id shaped like a random UUIDv4 (version and variant bits forced)."""
    digest = bytearray(
        hashlib.sha256("\0".join((domain, *parts)).encode("utf-8")).digest()[:16]
    )
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def _is_uuid4(value: object) -> bool:
    return isinstance(value, str) and _UUID4.fullmatch(value) is not None


def _strip_cache_control(value):
    if isinstance(value, dict):
        return {
            k: _strip_cache_control(v) for k, v in value.items() if k != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_cache_control(v) for v in value]
    return value


def derive_session_id(device_id: str, body: dict) -> str:
    """Session id stable for a conversation: ``device_id`` + canonical ``messages[0]``.

    ``cache_control`` is ignored because clients move it between turns. Clients
    that trim or compact old messages get a new id when ``messages[0]`` changes.
    """
    messages = body.get("messages")
    first = messages[0] if isinstance(messages, list) and messages else None
    first = _strip_cache_control(first)  # a copy: safe to rewrite below
    if isinstance(first, dict) and isinstance(first.get("content"), str):
        # SDKs turn string content into a text block to attach cache_control.
        first["content"] = [{"type": "text", "text": first["content"]}]
    canonical = json.dumps(first, sort_keys=True, separators=(",", ":"))
    return _uuid4_from("clove:session", device_id, canonical)


def _is_human_turn(message: object) -> bool:
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    content = message.get("content")
    if not isinstance(content, list):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def derive_prompt_id(session_id: str, body: dict) -> str:
    """Prompt id that changes per human turn and holds across a tool loop.

    A human turn is a user message without any ``tool_result`` block.
    """
    messages = body.get("messages")
    turns = sum(map(_is_human_turn, messages)) if isinstance(messages, list) else 0
    return _uuid4_from("clove:prompt", session_id, str(turns))


def _is_context_reminder(text: str) -> bool:
    return text.startswith("<system-reminder>") and "</system-reminder>" in text


def fingerprint_message_text(body: dict) -> str:
    """The text the CLI samples for the ``cc_version`` suffix.

    First user message: its string content, else its first ``text`` block that
    is not a ``<system-reminder>…</system-reminder>`` block; ``""`` otherwise.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    text = text if isinstance(text, str) else ""
                    if not _is_context_reminder(text):
                        return text
        return ""
    return ""


def version_suffix(text: str, version: str) -> str:
    """The 3-hex build suffix of ``cc_version``.

    ``sha256(salt + t[4] + t[7] + t[20] + version)[:3]`` over UTF-16 code units,
    ``"0"`` for a missing index; lone surrogates hash as U+FFFD (as Node does).
    """
    encoded = text[: max(_SUFFIX_INDICES) + 1].encode("utf-16-le", "surrogatepass")
    units = [
        encoded[2 * i : 2 * i + 2]
        if 2 * i + 2 <= len(encoded)
        else "0".encode("utf-16-le")
        for i in _SUFFIX_INDICES
    ]
    sampled = b"".join(units).decode("utf-16-le", errors="replace")
    return hashlib.sha256(
        (_SUFFIX_SALT + sampled + version).encode("utf-8")
    ).hexdigest()[:3]


# --------------------------------------------------------------------------
# Body finalization
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FinalizedBody:
    body_bytes: bytes
    session_id: str
    prompt_id: str
    cli_version: str


def _reject_constant(name: str):
    raise ValueError(f"non-finite number {name}")


def _client_user_id(metadata: object) -> dict:
    """The client's ``metadata.user_id`` when it is a JSON object string, else ``{}``."""
    if not isinstance(metadata, dict) or not isinstance(metadata.get("user_id"), str):
        return {}
    try:
        parsed = json.loads(metadata["user_id"], parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _adopted_session_id(client_user_id: dict) -> str | None:
    """The client's UUIDv4 ``session_id`` (lowercased), if it sent one."""
    session_id = client_user_id.get("session_id")
    return session_id.lower() if _is_uuid4(session_id) else None


def effective_session_id(device_id: str, body: dict) -> str:
    """The session id ``finalize_messages_body`` sends for ``body``.

    The client's UUIDv4 ``session_id`` from a JSON ``metadata.user_id`` when it
    has one, else ``derive_session_id``.
    """
    adopted = _adopted_session_id(_client_user_id(body.get("metadata")))
    return adopted or derive_session_id(device_id, body)


def _system_blocks(
    system: object, *, include_identity: bool, billing_text: str
) -> list:
    """``[billing, identity?, *client blocks]`` with client billing blocks dropped."""
    if isinstance(system, str):
        client_blocks = [{"type": "text", "text": system}] if system else []
    elif system is None:
        client_blocks = []
    elif isinstance(system, list):
        client_blocks = [
            block
            for block in system
            if not (
                isinstance(block, dict)
                and isinstance(block.get("text"), str)
                and block["text"].startswith(_BILLING_MARKER)
            )
        ]
    else:
        raise ValueError(
            f"system must be a string or a list, not {type(system).__name__}"
        )
    leading_identity = (
        bool(client_blocks)
        and isinstance(client_blocks[0], dict)
        and client_blocks[0].get("type") == "text"
        and client_blocks[0].get("text") == IDENTITY_TEXT
    )
    ours = [{"type": "text", "text": billing_text}]
    if include_identity and not leading_identity:
        ours.append({"type": "text", "text": IDENTITY_TEXT})
    return ours + client_blocks


def _with_cli_key_order(body: dict, system: list, metadata: dict) -> dict:
    """Replace ``system``/``metadata`` in place; insert missing ones where the CLI puts them."""
    items = []
    for key, value in body.items():
        if key == "system":
            value = system
        elif key == "metadata":
            value = metadata
        items.append((key, value))
        if key == "messages" and "system" not in body:
            items.append(("system", system))
    if "system" not in body and "messages" not in body:
        items.append(("system", system))
    if "metadata" not in body:
        anchor = "tools" if "tools" in body else "system"
        index = next(i for i, (key, _) in enumerate(items) if key == anchor)
        items.insert(index + 1, ("metadata", metadata))
    return dict(items)


def finalize_messages_body(
    body: dict,
    *,
    device_id: str,
    account_uuid: str,
    session_id: str | None = None,
    client_prompt_id: str | None = None,
    prev_request_id: str | None = None,
    include_identity: bool = True,
) -> FinalizedBody:
    """Give a client ``/v1/messages`` body the CLI's identity and serialize it.

    Adds the billing block (and the identity block unless ``include_identity``
    is false or the client already leads with it), rewrites
    ``metadata.user_id``, and signs ``cch``. Every other field, known or not,
    is kept with its client order; ``body`` itself is never mutated. A client's
    UUIDv4 ``session_id`` (in a JSON ``metadata.user_id``, whose extra keys are
    then kept after ours) and ``client_prompt_id`` are adopted, otherwise both
    are derived. ``session_id``, when given, is sent as is and must be
    ``effective_session_id(device_id, body)``; a caller that needs the id before
    finalizing passes it so it is not computed twice. ``prev_request_id`` (the
    previous response's ``request-id``) becomes ``cc_prev_req``. Raises
    ``ValueError`` for NaN or infinite numbers and for a ``system`` that is
    neither a string nor a list.
    """
    client_metadata = body.get("metadata")
    client_user_id = _client_user_id(client_metadata)
    adopted_session_id = _adopted_session_id(client_user_id)
    if session_id is None:
        session_id = adopted_session_id or derive_session_id(device_id, body)
    if _is_uuid4(client_prompt_id):
        prompt_id = client_prompt_id.lower()
    else:
        prompt_id = derive_prompt_id(session_id, body)

    version = cli_version()
    suffix = version_suffix(fingerprint_message_text(body), version)
    prev_req = ""
    if prev_request_id:
        if _REQUEST_ID.fullmatch(prev_request_id):
            prev_req = f" cc_prev_req={prev_request_id};"
        else:
            logger.warning(
                f"Dropping malformed previous request id {prev_request_id!r}"
            )
    billing_text = (
        f"{BILLING_PREFIX}cc_version={version}.{suffix}; cc_entrypoint=cli; "
        f"cch={_CCH_PLACEHOLDER};{prev_req} cc_prompt_id={prompt_id}; cc_turn_origin=human;"
    )
    system = _system_blocks(
        body.get("system"), include_identity=include_identity, billing_text=billing_text
    )

    user_id = {
        "device_id": device_id,
        "account_uuid": account_uuid,
        "session_id": session_id,
    }
    if adopted_session_id:
        for key, value in client_user_id.items():
            user_id.setdefault(key, value)
    metadata = dict(client_metadata) if isinstance(client_metadata, dict) else {}
    metadata["user_id"] = json.dumps(user_id, separators=(",", ":"), ensure_ascii=False)

    unsigned = serialize(_with_cli_key_order(body, system, metadata))
    offset, cch = _sign(unsigned)
    if offset is None:  # we just wrote the billing block; missing means a bug here
        raise RuntimeError("billing cch placeholder not found in finalized body")
    signed = unsigned[:offset] + cch.encode() + unsigned[offset + len(cch) :]
    return FinalizedBody(
        body_bytes=signed,
        session_id=session_id,
        prompt_id=prompt_id,
        cli_version=version,
    )


def verify_billing(raw_body: bytes) -> tuple[bool, bool]:
    """Check a captured body's billing block: (cch matches, cc_version suffix matches)."""
    offset, cch = _sign(raw_body)
    if offset is None:
        return False, False
    cch_ok = raw_body[offset : offset + len(cch)] == cch.encode()
    body = json.loads(raw_body)
    match = _BILLING_VERSION.search(body["system"][0]["text"])
    suffix_ok = match is not None and match.group(2) == version_suffix(
        fingerprint_message_text(body), match.group(1)
    )
    return cch_ok, suffix_ok


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------


def _merge_betas(client_beta_header: str | None) -> str:
    betas = list(BASE_BETAS)
    for beta in (client_beta_header or "").split(","):
        beta = beta.strip()
        if beta and beta not in betas:
            betas.append(beta)
    return ",".join(betas)


def _common_headers(
    access_token: str, client_beta_header: str | None, cli_version: str
) -> dict[str, str]:
    return {
        **STATIC_HEADERS,
        **PLATFORM_HEADERS,
        "Authorization": f"Bearer {access_token}",
        "User-Agent": _USER_AGENT_TEMPLATE.format(version=cli_version),
        "anthropic-beta": _merge_betas(client_beta_header),
        "x-client-request-id": str(uuid.uuid4()),
    }


def _in_cli_order(values: dict[str, str]) -> list[tuple[str, str]]:
    return [(name, values[name]) for name in HEADER_ORDER if name in values]


def build_messages_headers(
    access_token: str,
    *,
    client_beta_header: str | None,
    session_id: str,
    prompt_id: str,
    cli_version: str,
    request_class: str = "main",
) -> list[tuple[str, str]]:
    """Ordered ``/v1/messages`` headers; ``Host`` and ``Content-Length`` are the transport's."""
    values = _common_headers(access_token, client_beta_header, cli_version)
    values["X-Claude-Code-Session-Id"] = session_id
    values["x-claude-code-prompt-id"] = prompt_id
    values["x-claude-code-request-class"] = request_class
    return _in_cli_order(values)


_MESSAGES_ONLY_HEADERS = (
    "Content-Type",
    "X-Claude-Code-Session-Id",
    "x-claude-code-prompt-id",
    "x-claude-code-request-class",
)


def build_api_headers(
    access_token: str, *, client_beta_header: str | None = None, cli_version: str
) -> list[tuple[str, str]]:
    """Ordered headers for other API calls (``/v1/models``): no session, prompt,
    request class or content type."""
    values = _common_headers(access_token, client_beta_header, cli_version)
    for name in _MESSAGES_ONLY_HEADERS:
        values.pop(name, None)
    return _in_cli_order(values)
