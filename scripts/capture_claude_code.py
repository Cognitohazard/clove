#!/usr/bin/env python3
"""Capture the genuine Claude Code CLI's `/v1/messages` request and check
Clove's OAuth identity layer against it.

Runs the real linux-x64 Claude Code binary (downloaded from the npm registry,
executed under a throwaway HOME with a fake `sk-ant-oat01-` token) behind a
local mitmproxy MITM that never forwards anywhere -- every request is answered
locally, so this never reaches Anthropic or any real account, whichever
`--version` it captures.

    # capture live traffic and diff it against the committed fixture
    uv run python scripts/capture_claude_code.py --check

    # capture live traffic and (re)write the fixture
    uv run python scripts/capture_claude_code.py --write tests/fixtures/claude_code_fingerprint.json

    # send one request through Clove's own OAuth path, diff against the same fixture
    uv run python scripts/capture_claude_code.py --clove

`--check` and `--clove` exit non-zero on drift in a *required* field (the ones
listed in `build_fingerprint` without a leading underscore); informational
fields (cli_version, beta list, body keys, ...) are printed but never fail the
run. `--clove` needs `app/utils/claude_code_identity.py` (planned unit U1) and
the OAuth transport factory in `app/core/http_client.py` (planned unit U2);
until those land it exits with a clear "unavailable" message instead of a
traceback -- see the U2 call site below for the placeholder factory name.
"""

import argparse
import asyncio
import base64
import json
import os
import pty
import re
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
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
"""mitmdump addon: capture Claude Code's ClientHello + the main /v1/messages
request, answering every request locally so nothing reaches the real API.

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
    """A harness-level failure: capture, parsing, or a failed gate."""


class HarnessUnavailable(HarnessError):
    """A dependency from another planned unit (U1/U2) isn't available yet."""


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
        self.ca_cert_path = self._wait_for_ca_cert()
        return self

    def _wait_for_ca_cert(self, timeout: float = 30.0) -> Path:
        cert = self._confdir / "mitmproxy-ca-cert.pem"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cert.exists():
                return cert
            if self._proc.poll() is not None:
                raise HarnessError(
                    "mitmdump exited before generating its CA certificate"
                )
            time.sleep(0.5)
        raise HarnessError(f"timed out waiting for {cert}")

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


# --------------------------------------------------------------------------
# Claude Code CLI download + interactive run
# --------------------------------------------------------------------------


def _resolve_version(version: str) -> str:
    if version != "latest":
        if not _VERSION_RE.match(version):
            raise HarnessError(
                f"invalid --version {version!r}, expected X.Y.Z or 'latest'"
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


def _download_cli_binary(version: str, dest_dir: Path) -> Path:
    with urllib.request.urlopen(
        f"{NPM_REGISTRY}/{PLATFORM_PACKAGE}/{version}", timeout=20
    ) as resp:
        meta = json.load(resp)
    tarball_url = meta["dist"]["tarball"]
    tarball_path = dest_dir / "package.tgz"
    with (
        urllib.request.urlopen(tarball_url, timeout=180) as resp,
        open(tarball_path, "wb") as f,
    ):
        shutil.copyfileobj(resp, f)
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
    """Split headers into (static_headers, platform_headers) per D8.

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
        raise HarnessError(f"unrecognized User-Agent: {value!r}")
    return "claude-cli/{version} (external, cli)"


def _betas(value: str) -> list[str]:
    return [b.strip() for b in value.split(",") if b.strip()]


def _parse_system_blocks(body: dict) -> tuple[dict, dict, dict]:
    system = body.get("system")
    if not (isinstance(system, list) and len(system) >= 2):
        raise HarnessError(
            f"expected system to be a list of >=2 blocks, got {type(system).__name__}"
        )
    billing, identity = system[0], system[1]
    billing_text = billing.get("text", "")
    if not billing_text.startswith(_BILLING_PREFIX):
        raise HarnessError(
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
        raise HarnessError("metadata.user_id is missing or not a JSON string")
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
            "Regenerate with scripts/capture_claude_code.py --write."
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


def capture_via_cli(version: str, timeout: float) -> tuple[dict, bytes, str]:
    resolved = _resolve_version(version)
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


async def _send_clove_request(session, headers: dict, body_bytes: bytes) -> None:
    await session.request(
        "POST",
        "https://api.anthropic.com/v1/messages?beta=true",
        headers=headers,
        data=body_bytes,
    )


def capture_via_clove(timeout: float) -> tuple[dict, bytes]:
    try:
        from app.utils.claude_code_identity import (
            build_messages_headers,
            finalize_messages_body,
        )
    except ImportError as exc:
        raise HarnessUnavailable(
            "app.utils.claude_code_identity is not available yet (planned unit U1)"
        ) from exc
    try:
        # D9's OAuth transport factory name/signature isn't fixed by the plan
        # yet -- this is a best-effort placeholder for planned unit U2.
        # Adjust the import and the create_oauth_session(...) call below once
        # U2 lands with its real factory.
        from app.core.http_client import create_oauth_session
    except ImportError as exc:
        raise HarnessUnavailable(
            "app.core.http_client has no OAuth transport factory yet (planned unit U2)"
        ) from exc

    body = {
        "model": "claude-sonnet-4-5-20250929",
        "max_tokens": 1,
        "stream": False,
        "messages": [{"role": "user", "content": "Say hi"}],
    }
    device_id = secrets.token_hex(32)
    finalized = finalize_messages_body(body, device_id=device_id, account_uuid="")
    headers = dict(
        build_messages_headers(
            "sk-ant-oat01-FAKE",
            client_beta_header=None,
            session_id=finalized.session_id,
            prompt_id=finalized.prompt_id,
        )
    )

    with tempfile.TemporaryDirectory(prefix="clove-cc-capture-") as tmp:
        tmp_path = Path(tmp)
        confdir = tmp_path / "mitmconf"
        capture_log = tmp_path / "capture.jsonl"
        addon_path = tmp_path / "capture_addon.py"
        _write_addon(addon_path)
        port = _free_port()

        with _MitmCapture(port, confdir, addon_path, capture_log) as mitm:
            session = create_oauth_session(
                proxy=f"http://127.0.0.1:{port}", verify=str(mitm.ca_cert_path)
            )
            try:
                asyncio.run(_send_clove_request(session, headers, finalized.body_bytes))
            finally:
                asyncio.run(session.close())

        request_rec, hello_rec = _select_main(_read_records(capture_log))
        raw_body = base64.b64decode(request_rec["raw_body_b64"])
        fingerprint = build_fingerprint(
            hello_rec, request_rec, cli_version="clove", raw_body=raw_body
        )
        return fingerprint, raw_body


# --------------------------------------------------------------------------
# Diffing and the billing gate
# --------------------------------------------------------------------------


def diff_fields(
    candidate: dict, fixture: dict, path: str = ""
) -> tuple[list[str], list[str]]:
    """Return (required_diffs, informational_notes). A key starting with `_`
    is informational at any nesting depth."""
    required: list[str] = []
    informational: list[str] = []
    for key in sorted(set(candidate) | set(fixture)):
        key_path = f"{path}.{key}" if path else key
        bucket = informational if key.startswith("_") else required
        if key not in candidate:
            bucket.append(f"{key_path}: missing from candidate")
            continue
        if key not in fixture:
            bucket.append(f"{key_path}: missing from fixture (new field)")
            continue
        left, right = candidate[key], fixture[key]
        if isinstance(left, dict) and isinstance(right, dict):
            sub_required, sub_informational = diff_fields(left, right, key_path)
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
            "skipped (app.utils.claude_code_identity unavailable -- planned unit U1)",
            True,
        )
    cch_ok, suffix_ok = verify_billing(raw_body)
    return f"cch_ok={cch_ok} suffix_ok={suffix_ok}", cch_ok and suffix_ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def cmd_write(args: argparse.Namespace) -> int:
    fingerprint, raw_body, cli_version = capture_via_cli(args.version, args.timeout)
    status, billing_ok = _billing_gate(raw_body)
    print(f"billing verification: {status}")
    if not billing_ok:
        print("refusing to write: billing verification failed", file=sys.stderr)
        return 1
    out_path = Path(args.write)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(fingerprint, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote fingerprint ({cli_version}) to {out_path}")
    return 0


def _check_against_fixture(
    fingerprint: dict, raw_body: bytes, fixture_path: Path, label: str
) -> int:
    if not fixture_path.exists():
        print(f"fixture not found: {fixture_path}", file=sys.stderr)
        return 1
    fixture = json.loads(fixture_path.read_text())
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
        return 1
    print(f"OK: {label} capture matches {fixture_path.name} on all required fields")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    fingerprint, raw_body, _ = capture_via_cli(args.version, args.timeout)
    return _check_against_fixture(fingerprint, raw_body, Path(args.fixture), "cli")


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
        help="Claude Code npm version to capture (default: latest)",
    )
    parser.add_argument(
        "--fixture",
        default=str(DEFAULT_FIXTURE),
        help="fixture path for --check/--clove",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait for the main request",
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
        return 2
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
