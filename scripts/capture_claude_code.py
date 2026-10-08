#!/usr/bin/env python3
"""Capture the genuine Claude Code CLI's `/v1/messages` request and check
Clove's OAuth identity layer against it.

Runs the real linux-x64 Claude Code binary (downloaded from the npm registry,
executed under a throwaway HOME with a fake `sk-ant-oat01-` token) behind a
local mitmproxy MITM that never forwards anywhere -- every request is answered
locally, so this never reaches Anthropic or any real account, whichever
`--version` it captures.

    # capture the fixture's own CLI version and diff it against the fixture
    # (the slice-close gate; only the daily CI job uses `latest`)
    uv run python scripts/capture_claude_code.py --check --version fixture

    # capture npm's latest CLI and diff it against the committed fixture
    uv run python scripts/capture_claude_code.py --check

    # capture live traffic and (re)write the fixture
    uv run python scripts/capture_claude_code.py --write tests/fixtures/claude_code_fingerprint.json

    # send Clove's own OAuth request through the same mitm, diff against the fixture
    uv run python scripts/capture_claude_code.py --clove

Exit codes: 0 match; 1 drift -- a *required* field differs (keys in
`build_fingerprint` without a leading underscore, at any depth), the billing
block fails `verify_billing`, or the captured request lost a shape the
extraction relies on (billing/identity blocks, User-Agent, `metadata.user_id`;
under `--clove` also a transport that accepts the mitm's certificate);
2 unavailable -- `--clove` cannot import `app.utils.claude_code_identity` /
`app.core.http_client.create_oauth_session`, or the OAuth transport has no
rnet (argparse's usage errors also exit 2); 3 harness error -- download,
integrity check, running the capture, reading its log, or a timeout; any
unexpected exception is a 3 too, never a 1. Informational fields
(cli_version, beta list, body keys, ...) are printed but never fail a run.

`--check` and `--write` need only the standard library plus `uvx` (mitmproxy
runs as `uvx --from mitmproxy mitmdump`); the billing check uses
`verify_billing` when the app is importable and is skipped otherwise.

`--clove` sends two requests through the mitm with Clove's OAuth transport and
its default trust roots -- no certificate override anywhere. Request 1 goes to
`https://api.anthropic.com/...`: mitmdump logs the ClientHello, then the
transport rejects the mitm certificate and the request fails, as intended.
Request 2 sends the identical headers and body to `http://api.anthropic.com/...`
through the same proxy, which the addon answers locally (it never connects
upstream), so its headers and body are captured with `Host: api.anthropic.com`.
TLS comes from request 1, everything else from request 2.

Each step has its own bound (registry and tarball socket timeouts, the mitm CA
wait, `--timeout` for the CLI's main request and for `--clove`); there is no
separate total timeout -- CI bounds the whole run with the job's
`timeout-minutes`.
"""

import argparse
import asyncio
import base64
import hashlib
import json
import os
import pty
import re
import secrets
import select
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "claude_code_fingerprint.json"
)

NPM_REGISTRY = "https://registry.npmjs.org"
CLI_PACKAGE = "@anthropic-ai/claude-code"
PLATFORM_PACKAGE = "@anthropic-ai/claude-code-linux-x64"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_UNAVAILABLE = 2
EXIT_HARNESS_ERROR = 3

# --clove: request 1 (TLS, fails on the mitm certificate) and request 2 (the
# same request over plain http, answered locally by the addon).
_CLOVE_TLS_URL = "https://api.anthropic.com/v1/messages?beta=true"
_CLOVE_PLAIN_URL = "http://api.anthropic.com/v1/messages?beta=true"

# TLS extension type numbers this harness needs raw bytes for.
_GROUPS_EXT = 10
_EC_POINTS_EXT = 11
_SIGALGS_EXT = 13
_SUPPORTED_VERSIONS_EXT = 43
_PSK_MODES_EXT = 45
# May or may not appear depending on ClientHello length / session resumption;
# excluded from extension_order so their presence doesn't register as drift.
_CONDITIONAL_EXTENSIONS = (21, 41)  # padding, pre_shared_key

_UA_RE = re.compile(r"^claude-cli/(?P<version>\S+) \(external, cli\)$")
_BILLING_PREFIX = "x-anthropic-billing-header: "
_BILLING_FIELD_RE = re.compile(r"(\w+)=")

# Headers whose values are secret, per-request, or already captured by a
# dedicated field -- excluded from static_headers/platform headers entirely.
_EXCLUDED_HEADERS = {
    "authorization",
    "host",
    "content-length",
    "user-agent",
    "anthropic-beta",
}
_PER_REQUEST_HEADERS = {
    "x-claude-code-session-id",
    "x-claude-code-prompt-id",
    "x-client-request-id",
}
_PLATFORM_HEADERS = {"x-stainless-arch", "x-stainless-os"}

_ADDON_SOURCE = '''\
"""mitmdump addon: log every ClientHello and every request (the CLI's, or
Clove's in --clove mode), answering every request locally -- https through the
MITM or plain http through the proxy -- so nothing reaches the real API.

Loaded by mitmdump's own bundled mitmproxy (via `uvx --from mitmproxy`), not
by capture_claude_code.py itself -- that's why this can `import mitmproxy`
even though the harness script is stdlib-only. Written to disk at runtime.
"""
import base64
import json
import os

from mitmproxy import http, tls

OUT = os.environ.get("CLOVE_CAPTURE_LOG", "capture.jsonl")

_SSE_EVENTS = [
    ("message_start", {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-fake",
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0,
                             "content_block": {"type": "text", "text": ""}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                             "delta": {"type": "text_delta", "text": "ok"}}),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                       "usage": {"output_tokens": 1}}),
    ("message_stop", {"type": "message_stop"}),
]
SSE = "".join(f"event: {e}\\ndata: {json.dumps(d)}\\n\\n" for e, d in _SSE_EVENTS)


def _append(record):
    with open(OUT, "a") as f:
        f.write(json.dumps(record) + "\\n")


def _key_share_groups(raw: bytes) -> list:
    groups, i = [], 2
    while i + 4 <= len(raw):
        group = int.from_bytes(raw[i:i + 2], "big")
        length = int.from_bytes(raw[i + 2:i + 4], "big")
        groups.append(f"0x{group:04x}")
        i += 4 + length
    return groups


def tls_clienthello(data: tls.ClientHelloData) -> None:
    hello = data.client_hello
    ext = dict(hello.extensions)
    _append({
        "kind": "clienthello",
        "client_id": data.context.client.id,
        "ciphers": [f"0x{c:04x}" for c in hello.cipher_suites],
        "extension_types": [t for t, _ in hello.extensions],
        "alpn": [p.decode() for p in hello.alpn_protocols],
        "ext_hex": {
            str(t): ext[t].hex()
            for t in (10, 11, 13, 43, 45)
            if t in ext
        },
        "key_share_groups": _key_share_groups(ext.get(51, b"")),
        "raw_hex": hello.raw_bytes(wrap_in_record=False).hex(),
    })


def request(flow: http.HTTPFlow) -> None:
    req = flow.request
    _append({
        "kind": "request",
        "client_id": flow.client_conn.id,
        "http_version": req.http_version,
        "url": req.pretty_url,
        "header_order": [k for k, _ in req.headers.items(multi=True)],
        "headers": list(req.headers.items(multi=True)),
        "raw_body_b64": base64.b64encode(req.raw_content or b"").decode(),
    })

    body_text = req.get_text(strict=False)
    try:
        parsed = json.loads(body_text)
    except ValueError:
        parsed = None

    if req.path.startswith("/v1/messages") and "count_tokens" not in req.path:
        if isinstance(parsed, dict) and parsed.get("stream"):
            flow.response = http.Response.make(
                200, SSE, {"content-type": "text/event-stream"}
            )
        else:
            flow.response = http.Response.make(200, json.dumps({
                "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-fake",
                "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
            }), {"content-type": "application/json"})
    elif "count_tokens" in req.path:
        flow.response = http.Response.make(
            200, \'{"input_tokens": 1}\', {"content-type": "application/json"}
        )
    else:
        flow.response = http.Response.make(
            404,
            \'{"type":"error","error":{"type":"not_found_error","message":"capture"}}\',
            {"content-type": "application/json"},
        )
'''


class HarnessError(Exception):
    """The harness itself failed (download, capture, parsing, timeout): exit 3."""


class HarnessUnavailable(HarnessError):
    """Clove's identity layer or OAuth transport can't be imported/used: exit 2."""


class CaptureDrift(HarnessError):
    """The captured request no longer has the shape the harness extracts from
    (CLI drift under --check, a Clove regression under --clove): exit 1."""


# --------------------------------------------------------------------------
# mitmproxy capture
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MitmCapture:
    """Starts/stops a `uvx --from mitmproxy mitmdump` subprocess running the
    embedded capture addon. Never uses pkill -- the Popen handle is tracked
    and terminated directly."""

    def __init__(self, port: int, confdir: Path, addon_path: Path, capture_log: Path):
        self.port = port
        self._confdir = confdir
        self._addon_path = addon_path
        self._capture_log = capture_log
        self._proc: subprocess.Popen | None = None
        self.ca_cert_path: Path | None = None

    def __enter__(self) -> "_MitmCapture":
        self._confdir.mkdir(parents=True, exist_ok=True)
        self._capture_log.touch()
        env = {**os.environ, "CLOVE_CAPTURE_LOG": str(self._capture_log)}
        self._proc = subprocess.Popen(
            [
                "uvx",
                "--from",
                "mitmproxy",
                "mitmdump",
                "-q",
                "-p",
                str(self.port),
                "--set",
                f"confdir={self._confdir}",
                "--set",
                "upstream_cert=false",
                "--set",
                "connection_strategy=lazy",
                "-s",
                str(self._addon_path),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            self.ca_cert_path = self._wait_until_ready()
        except BaseException:
            self.__exit__(None, None, None)  # `with` won't call it for us
            raise
        return self

    def _wait_until_ready(self, timeout: float = 120.0) -> Path:
        """Wait for the CA file *and* the listener: mitmdump writes the CA while
        configuring and binds the port later. Generous: a cold `uvx` cache
        installs mitmproxy first."""
        cert = self._confdir / "mitmproxy-ca-cert.pem"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise HarnessError("mitmdump exited during startup")
            if cert.exists():
                try:
                    socket.create_connection(("127.0.0.1", self.port), 1).close()
                    return cert
                except OSError:
                    pass
            time.sleep(0.5)
        # No path in the message: CI dedupes issue comments on this output.
        raise HarnessError(f"timed out after {timeout:.0f}s waiting for mitmdump")

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)


def _write_addon(path: Path) -> None:
    path.write_text(_ADDON_SOURCE)


def _read_records(path: Path) -> list[dict]:
    """Parse each JSONL line, skipping a partial trailing line still being
    written by the addon process (this is polled concurrently with its
    writes, so the last line can be incomplete)."""
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        lines = [line for line in f if line.strip()]
    for i, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if i != len(lines) - 1:
                raise
    return records


def _capture_has_main(capture_log: Path) -> bool:
    for rec in _read_records(capture_log):
        if rec.get("kind") == "request" and "/v1/messages" in rec.get("url", ""):
            headers_ci = {k.lower(): v for k, v in rec["headers"]}
            if headers_ci.get("x-claude-code-request-class") == "main":
                return True
    return False


def _select_main(records: list[dict]) -> tuple[dict, dict]:
    """Pick the request tagged `x-claude-code-request-class: main` and the
    ClientHello of the *same connection* -- the CLI opens other connections
    with a different (axios-style) ClientHello for side traffic."""
    hello_by_client = {
        r["client_id"]: r for r in records if r.get("kind") == "clienthello"
    }
    for rec in records:
        if rec.get("kind") != "request" or "/v1/messages" not in rec.get("url", ""):
            continue
        headers_ci = {k.lower(): v for k, v in rec["headers"]}
        if headers_ci.get("x-claude-code-request-class") != "main":
            continue
        hello = hello_by_client.get(rec["client_id"])
        if hello is None:
            raise HarnessError(
                "found the main request but no ClientHello for its connection"
            )
        return rec, hello
    raise HarnessError("no main /v1/messages request was captured")


def _select_clove(records: list[dict]) -> tuple[dict, dict]:
    """Pair `--clove`'s plain-http request (request 2) with the last ClientHello,
    which request 1 sent before rejecting the mitm certificate -- the two ran
    on different connections by design, so client ids can't pair them."""
    hellos = [r for r in records if r.get("kind") == "clienthello"]
    plain = [
        r
        for r in records
        if r.get("kind") == "request"
        and urllib.parse.urlsplit(r.get("url", ""))[:3]
        == ("http", "api.anthropic.com", "/v1/messages")
    ]
    if not hellos:
        raise HarnessError("--clove: the TLS request produced no ClientHello")
    if not plain:
        raise HarnessError("--clove: the plain-http request was not captured")
    return plain[-1], hellos[-1]


# --------------------------------------------------------------------------
# Claude Code CLI download + interactive run
# --------------------------------------------------------------------------


def _load_fixture(path: Path) -> dict:
    if not path.exists():
        raise HarnessError(f"fixture not found: {path}")
    return json.loads(path.read_text())


def _resolve_version(version: str, fixture_path: Path) -> str:
    if version == "fixture":
        pinned = _load_fixture(fixture_path).get("_cli_version", "")
        if not _VERSION_RE.match(pinned):
            raise HarnessError(f"fixture has no usable _cli_version: {pinned!r}")
        return pinned
    if version != "latest":
        if not _VERSION_RE.match(version):
            raise HarnessError(
                f"invalid --version {version!r}, expected X.Y.Z, 'latest' or 'fixture'"
            )
        return version
    with urllib.request.urlopen(
        f"{NPM_REGISTRY}/{CLI_PACKAGE}/latest", timeout=20
    ) as resp:
        data = json.load(resp)
    resolved = data["version"]
    if not _VERSION_RE.match(resolved):
        raise HarnessError(f"registry returned an unexpected version {resolved!r}")
    return resolved


def _verify_integrity(sha512_digest: bytes, integrity: str) -> None:
    """Raise unless npm's SRI `dist.integrity` string lists this sha512 digest."""
    expected = [
        token.removeprefix("sha512-")
        for token in integrity.split()
        if token.startswith("sha512-")
    ]
    if not expected:
        raise HarnessError(
            f"registry dist.integrity has no sha512 entry: {integrity!r}"
        )
    if base64.b64encode(sha512_digest).decode() not in expected:
        raise HarnessError(
            "tarball sha512 does not match the registry's dist.integrity"
        )


def _download_cli_binary(version: str, dest_dir: Path) -> Path:
    with urllib.request.urlopen(
        f"{NPM_REGISTRY}/{PLATFORM_PACKAGE}/{version}", timeout=20
    ) as resp:
        meta = json.load(resp)
    tarball_url = meta["dist"]["tarball"]
    tarball_path = dest_dir / "package.tgz"
    digest = hashlib.sha512()
    with (
        urllib.request.urlopen(tarball_url, timeout=180) as resp,
        open(tarball_path, "wb") as f,
    ):
        while chunk := resp.read(1 << 20):
            digest.update(chunk)
            f.write(chunk)
    _verify_integrity(digest.digest(), meta["dist"].get("integrity", ""))
    with tarfile.open(tarball_path) as tar:
        member = tar.getmember("package/claude")
        if member.issym() or member.islnk() or not member.isfile():
            raise HarnessError(f"unexpected tar member for package/claude: {member!r}")
        tar.extract(member, path=dest_dir, filter="data")
    binary_path = dest_dir / "package" / "claude"
    binary_path.chmod(0o755)
    return binary_path


def _seed_home(home: Path, workdir: Path) -> None:
    config = {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "numStartups": 5,
        "projects": {
            str(workdir): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
        },
    }
    text = json.dumps(config)
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".claude.json").write_text(text)
    (home / ".claude.json").write_text(text)


def _run_cli_interactively(
    binary: Path,
    home: Path,
    workdir: Path,
    proxy_port: int,
    ca_cert: Path,
    capture_log: Path,
    timeout: float,
) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "TERM": "xterm-256color",
        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
        "NODE_EXTRA_CA_CERTS": str(ca_cert),
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-FAKEFAKEFAKE",
        "DISABLE_AUTOUPDATER": "1",
        "COLUMNS": "120",
        "LINES": "40",
    }

    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(workdir)
        os.execve(str(binary), ["claude", "Say hi"], env)
        os._exit(127)  # only reached if execve failed

    deadline = time.monotonic() + timeout
    found = False
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 1.0)
            if master_fd in ready:
                try:
                    if not os.read(master_fd, 65536):
                        break
                except OSError:
                    break
            if _capture_has_main(capture_log):
                found = True
                break
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    if not found:
        raise HarnessError(
            f"timed out after {timeout}s waiting for the main /v1/messages request"
        )


# --------------------------------------------------------------------------
# Fingerprint extraction
# --------------------------------------------------------------------------


def _is_grease(value: int) -> bool:
    return (value & 0x0F0F) == 0x0A0A


def _parse_group_list(hex_str: str) -> list[int]:
    data = bytes.fromhex(hex_str)
    count = int.from_bytes(data[:2], "big")
    return [int.from_bytes(data[2 + i : 4 + i], "big") for i in range(0, count, 2)]


def _compression_methods_hex(raw_hello: bytes) -> str:
    """The ClientHello's compression_methods vector (length byte included, like
    the *_ext_hex fields). `raw_hello` is the handshake body: legacy_version(2)
    random(32) session_id<1> cipher_suites<2> compression_methods<1> ..."""
    i = 2 + 32
    i += 1 + raw_hello[i]  # session_id
    i += 2 + int.from_bytes(raw_hello[i : i + 2], "big")  # cipher_suites
    end = i + 1 + raw_hello[i]
    if end > len(raw_hello):
        raise HarnessError("truncated ClientHello: compression_methods overruns it")
    return raw_hello[i:end].hex()


def _build_tls(hello: dict) -> dict:
    ciphers = [int(c, 16) for c in hello["ciphers"]]
    ext_types = hello["extension_types"]
    ext_hex = hello["ext_hex"]
    groups = (
        _parse_group_list(ext_hex[str(_GROUPS_EXT)])
        if str(_GROUPS_EXT) in ext_hex
        else []
    )
    grease = (
        any(_is_grease(c) for c in ciphers)
        or any(_is_grease(t) for t in ext_types)
        or any(_is_grease(g) for g in groups)
    )
    return {
        "ciphers": hello["ciphers"],
        "compression_methods_hex": _compression_methods_hex(
            bytes.fromhex(hello["raw_hex"])
        ),
        "extension_order": [t for t in ext_types if t not in _CONDITIONAL_EXTENSIONS],
        "_conditional_extensions_note": (
            "21 (padding) and 41 (pre_shared_key) may appear depending on hello "
            "length / resumption; excluded from extension_order"
        ),
        "alpn": hello["alpn"],
        "grease": grease,
        "supported_groups_ext_hex": ext_hex.get(str(_GROUPS_EXT), ""),
        "signature_algorithms_ext_hex": ext_hex.get(str(_SIGALGS_EXT), ""),
        "supported_versions_ext_hex": ext_hex.get(str(_SUPPORTED_VERSIONS_EXT), ""),
        "ec_point_formats_ext_hex": ext_hex.get(str(_EC_POINTS_EXT), ""),
        "psk_modes_ext_hex": ext_hex.get(str(_PSK_MODES_EXT), ""),
        "key_share_groups": hello["key_share_groups"],
    }


def _classify_headers(
    header_order: list[str], header_values: dict[str, str]
) -> tuple[dict, dict]:
    """Split headers into (static_headers, platform_headers).

    Per-request UUID headers, Authorization, Host, Content-Length, User-Agent
    and anthropic-beta are excluded: their values are secret, per-request, or
    already captured by a dedicated field (user_agent_template, the betas
    list, billing_block/identity_block).
    """
    static_headers: dict[str, str] = {}
    platform_headers: dict[str, str] = {}
    for name in header_order:
        lower = name.lower()
        if lower in _PER_REQUEST_HEADERS or lower in _EXCLUDED_HEADERS:
            continue
        if lower in _PLATFORM_HEADERS:
            platform_headers[name] = header_values[name]
        else:
            static_headers[name] = header_values[name]
    return static_headers, platform_headers


def _user_agent_template(value: str) -> str:
    if not _UA_RE.match(value):
        raise CaptureDrift(f"unrecognized User-Agent: {value!r}")
    return "claude-cli/{version} (external, cli)"


def _betas(value: str) -> list[str]:
    return [b.strip() for b in value.split(",") if b.strip()]


def _parse_system_blocks(body: dict) -> tuple[dict, dict, dict]:
    system = body.get("system")
    if not (isinstance(system, list) and len(system) >= 2):
        raise CaptureDrift(
            f"expected system to be a list of >=2 blocks, got {type(system).__name__}"
        )
    billing, identity = system[0], system[1]
    billing_text = billing.get("text", "")
    if not billing_text.startswith(_BILLING_PREFIX):
        raise CaptureDrift(
            f"system[0] doesn't start with the billing prefix: {billing_text[:40]!r}"
        )
    fields_text = billing_text[len(_BILLING_PREFIX) :].rstrip(";")
    pairs = dict(pair.split("=", 1) for pair in fields_text.split("; ") if "=" in pair)
    billing_block = {
        "index": 0,
        "prefix": _BILLING_PREFIX,
        "fields": _BILLING_FIELD_RE.findall(fields_text),
        "cc_entrypoint": pairs.get("cc_entrypoint"),
        "cc_turn_origin": pairs.get("cc_turn_origin"),
        "cache_control": billing.get("cache_control"),
    }
    identity_block = {
        "index": 1,
        "text": identity.get("text"),
        "cache_control": identity.get("cache_control"),
    }
    return billing_block, identity_block, pairs


def _parse_user_id(body: dict) -> tuple[list[str], dict]:
    metadata = body.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("user_id"), str):
        raise CaptureDrift("metadata.user_id is missing or not a JSON string")
    parsed = json.loads(metadata["user_id"])
    return list(parsed.keys()), parsed


def _build_body_fields(
    body: dict, raw_body: bytes, header_values_ci: dict[str, str]
) -> dict:
    billing_block, identity_block, billing_pairs = _parse_system_blocks(body)
    user_id_keys, user_id = _parse_user_id(body)

    recompacted = json.dumps(
        json.loads(raw_body), separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")

    return {
        "billing_block": billing_block,
        "identity_block": identity_block,
        "user_id_keys": user_id_keys,
        "prompt_id_equals_billing_cc_prompt_id": (
            header_values_ci.get("x-claude-code-prompt-id")
            == billing_pairs.get("cc_prompt_id")
        ),
        "session_id_equals_user_id_session": (
            header_values_ci.get("x-claude-code-session-id")
            == user_id.get("session_id")
        ),
        "json_compact": recompacted == raw_body,
        "_betas_main": _betas(header_values_ci.get("anthropic-beta", "")),
        "_body_keys_main": sorted(body.keys()),
    }


def build_fingerprint(
    hello: dict, request_rec: dict, *, cli_version: str, raw_body: bytes
) -> dict:
    """Build the structural fingerprint dict for one captured /v1/messages
    request. Keys without a leading underscore are *required* (compared for
    exact equality against the fixture); underscore-prefixed keys are
    informational (printed on drift, never fail a run)."""
    header_order = request_rec["header_order"]
    header_values = dict(request_rec["headers"])
    header_values_ci = {k.lower(): v for k, v in header_values.items()}

    static_headers, platform_headers = _classify_headers(header_order, header_values)
    body = json.loads(raw_body)

    return {
        "_comment": (
            "Structural fingerprint of Claude Code's main /v1/messages request "
            "(OAuth, interactive cli entrypoint). No prompt text, no paths. "
            "Regenerate with scripts/capture_claude_code.py --write. Never read "
            "at runtime: the constants in app/utils/claude_code_identity.py are "
            "the source of truth and a test pins them to this file."
        ),
        "_cli_version": cli_version,
        "tls": _build_tls(hello),
        "http_version": request_rec["http_version"],
        "header_order": header_order,
        "static_headers": static_headers,
        "_platform_headers_captured": platform_headers,
        "user_agent_template": _user_agent_template(header_values_ci["user-agent"]),
        "url_query": urllib.parse.urlsplit(request_rec["url"]).query,
        **_build_body_fields(body, raw_body, header_values_ci),
    }


# --------------------------------------------------------------------------
# Capture orchestration
# --------------------------------------------------------------------------


def capture_via_cli(
    version: str, timeout: float, fixture_path: Path
) -> tuple[dict, bytes, str]:
    resolved = _resolve_version(version, fixture_path)
    with tempfile.TemporaryDirectory(prefix="clove-cc-capture-") as tmp:
        tmp_path = Path(tmp)
        home = tmp_path / "home"
        workdir = tmp_path / "work"
        confdir = tmp_path / "mitmconf"
        home.mkdir()
        workdir.mkdir()
        capture_log = tmp_path / "capture.jsonl"
        addon_path = tmp_path / "capture_addon.py"
        _write_addon(addon_path)
        _seed_home(home, workdir)

        binary = _download_cli_binary(resolved, tmp_path)
        port = _free_port()

        with _MitmCapture(port, confdir, addon_path, capture_log) as mitm:
            _run_cli_interactively(
                binary, home, workdir, port, mitm.ca_cert_path, capture_log, timeout
            )

        request_rec, hello_rec = _select_main(_read_records(capture_log))
        raw_body = base64.b64decode(request_rec["raw_body_b64"])
        fingerprint = build_fingerprint(
            hello_rec, request_rec, cli_version=resolved, raw_body=raw_body
        )
        return fingerprint, raw_body, resolved


async def _send_clove_requests(
    create_oauth_session,
    proxy_url: str,
    headers: list[tuple[str, str]],
    body_bytes: bytes,
) -> Exception:
    """Request 1 (TLS) must fail on the mitm certificate after the ClientHello
    is logged; request 2 (plain http, same headers/body) is answered locally.
    Returns request 1's exception, for diagnosing a missing ClientHello."""
    try:
        session = create_oauth_session(proxy=proxy_url)
    except RuntimeError as exc:  # U2 contract: raised when rnet is missing
        raise HarnessUnavailable(
            f"Clove's OAuth transport is unavailable: {exc}"
        ) from exc
    try:
        try:
            await session.request(
                "POST", _CLOVE_TLS_URL, headers=headers, data=body_bytes
            )
        except Exception as exc:  # expected: the trust roots reject the mitm cert
            tls_error = exc
        else:
            raise CaptureDrift(
                "--clove: the OAuth transport accepted the mitm's untrusted "
                "certificate -- it must verify against the system trust roots"
            )
        await session.request(
            "POST", _CLOVE_PLAIN_URL, headers=headers, data=body_bytes
        )
    finally:
        await session.close()
    return tls_error


def capture_via_clove(timeout: float) -> tuple[dict, bytes]:
    try:
        from app.utils.claude_code_identity import (
            build_messages_headers,
            finalize_messages_body,
        )
    except ImportError as exc:
        raise HarnessUnavailable(
            "app.utils.claude_code_identity is not available (unit U1)"
        ) from exc
    try:
        from app.core.http_client import create_oauth_session
    except ImportError as exc:
        raise HarnessUnavailable(
            "app.core.http_client.create_oauth_session is not available (unit U2)"
        ) from exc

    body = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1,
        "stream": False,
        "messages": [{"role": "user", "content": "Say hi"}],
    }
    finalized = finalize_messages_body(
        body, device_id=secrets.token_hex(32), account_uuid=""
    )
    headers = build_messages_headers(
        "sk-ant-oat01-FAKE",
        client_beta_header=None,
        session_id=finalized.session_id,
        prompt_id=finalized.prompt_id,
        cli_version=finalized.cli_version,
    )

    with tempfile.TemporaryDirectory(prefix="clove-cc-capture-") as tmp:
        tmp_path = Path(tmp)
        capture_log = tmp_path / "capture.jsonl"
        addon_path = tmp_path / "capture_addon.py"
        _write_addon(addon_path)
        port = _free_port()

        with _MitmCapture(port, tmp_path / "mitmconf", addon_path, capture_log):
            send = _send_clove_requests(
                create_oauth_session,
                f"http://127.0.0.1:{port}",
                headers,
                finalized.body_bytes,
            )
            try:
                tls_error = asyncio.run(asyncio.wait_for(send, timeout))
            except TimeoutError as exc:
                raise HarnessError(
                    f"--clove: timed out after {timeout:.0f}s sending the requests"
                ) from exc

        try:
            request_rec, hello_rec = _select_clove(_read_records(capture_log))
        except HarnessError as exc:
            raise HarnessError(f"{exc} (request 1 raised {tls_error!r})") from exc
        raw_body = base64.b64decode(request_rec["raw_body_b64"])
        fingerprint = build_fingerprint(
            hello_rec, request_rec, cli_version=finalized.cli_version, raw_body=raw_body
        )
        return fingerprint, raw_body


# --------------------------------------------------------------------------
# Diffing and the billing gate
# --------------------------------------------------------------------------


def diff_fields(
    candidate: dict, fixture: dict, path: str = "", informational_parent: bool = False
) -> tuple[list[str], list[str]]:
    """Return (required_diffs, informational_notes). A key starting with `_`
    is informational at any nesting depth, and so is everything under it."""
    required: list[str] = []
    informational: list[str] = []
    for key in sorted(set(candidate) | set(fixture)):
        key_path = f"{path}.{key}" if path else key
        is_informational = informational_parent or key.startswith("_")
        bucket = informational if is_informational else required
        if key not in candidate:
            bucket.append(f"{key_path}: missing from candidate")
            continue
        if key not in fixture:
            bucket.append(f"{key_path}: missing from fixture (new field)")
            continue
        left, right = candidate[key], fixture[key]
        if isinstance(left, dict) and isinstance(right, dict):
            sub_required, sub_informational = diff_fields(
                left, right, key_path, is_informational
            )
            required.extend(sub_required)
            informational.extend(sub_informational)
        elif left != right:
            bucket.append(f"{key_path}: fixture={right!r} candidate={left!r}")
    return required, informational


def _billing_gate(raw_body: bytes) -> tuple[str, bool]:
    try:
        from app.utils.claude_code_identity import verify_billing
    except ImportError:
        return (
            "skipped (app.utils.claude_code_identity is not available (unit U1))",
            True,
        )
    cch_ok, suffix_ok = verify_billing(raw_body)
    return f"cch_ok={cch_ok} suffix_ok={suffix_ok}", cch_ok and suffix_ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_write(args: argparse.Namespace) -> int:
    fingerprint, raw_body, cli_version = capture_via_cli(
        args.version, args.timeout, Path(args.fixture)
    )
    status, billing_ok = _billing_gate(raw_body)
    print(f"billing verification: {status}")
    if not billing_ok:
        print("refusing to write: billing verification failed", file=sys.stderr)
        return EXIT_DRIFT
    out_path = Path(args.write)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fingerprint, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote fingerprint ({cli_version}) to {out_path}")
    print(
        "now update the constants in app/utils/claude_code_identity.py to match; "
        "tests pin them to the fixture"
    )
    return EXIT_OK


def _check_against_fixture(
    fingerprint: dict, raw_body: bytes, fixture_path: Path, label: str
) -> int:
    fixture = _load_fixture(fixture_path)
    required_diffs, informational = diff_fields(fingerprint, fixture)
    for note in informational:
        print(f"informational: {note}")
    status, billing_ok = _billing_gate(raw_body)
    print(f"billing verification: {status}")
    if required_diffs:
        print(f"required field drift ({label}):")
        for d in required_diffs:
            print(f"  {d}")
    if required_diffs or not billing_ok:
        return EXIT_DRIFT
    print(f"OK: {label} capture matches {fixture_path.name} on all required fields")
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    fixture_path = Path(args.fixture)
    fingerprint, raw_body, _ = capture_via_cli(args.version, args.timeout, fixture_path)
    return _check_against_fixture(fingerprint, raw_body, fixture_path, "cli")


def cmd_clove(args: argparse.Namespace) -> int:
    fingerprint, raw_body = capture_via_clove(args.timeout)
    return _check_against_fixture(fingerprint, raw_body, Path(args.fixture), "clove")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", metavar="PATH", help="capture live and write the fingerprint to PATH"
    )
    mode.add_argument(
        "--check", action="store_true", help="capture live and diff against --fixture"
    )
    mode.add_argument(
        "--clove",
        action="store_true",
        help="send one request through Clove's own OAuth path and diff against --fixture",
    )
    parser.add_argument(
        "--version",
        default="latest",
        help="Claude Code npm version to capture: X.Y.Z, 'latest' (default) or "
        "'fixture' (the fixture's _cli_version)",
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE),
        help="fixture path for --check/--clove and for --version fixture",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait for the CLI's main request, or for --clove's requests",
    )
    args = parser.parse_args(argv)

    try:
        if args.write:
            return cmd_write(args)
        if args.check:
            return cmd_check(args)
        return cmd_clove(args)
    except HarnessUnavailable as exc:
        print(f"unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except CaptureDrift as exc:
        print(f"drift: {exc}", file=sys.stderr)
        return EXIT_DRIFT
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return EXIT_HARNESS_ERROR
    except Exception:  # anything unexpected is a broken harness, never "drift"
        traceback.print_exc()
        return EXIT_HARNESS_ERROR


if __name__ == "__main__":
    sys.exit(main())
