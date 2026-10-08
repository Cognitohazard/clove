"""Pure-function tests for ``scripts/capture_claude_code.py`` (no network, no subprocess)."""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
harness = importlib.import_module("capture_claude_code")


def _raw_hello(
    *, session_id: bytes = b"", ciphers=(0x1301,), compression: bytes = b"\x00"
) -> str:
    """A minimal ClientHello handshake body, hex-encoded like the addon's ``raw_hex``."""
    suites = b"".join(c.to_bytes(2, "big") for c in ciphers)
    body = (
        b"\x03\x03"
        + bytes(32)
        + bytes([len(session_id)])
        + session_id
        + len(suites).to_bytes(2, "big")
        + suites
        + bytes([len(compression)])
        + compression
        + b"\x00\x00"  # empty extensions block
    )
    return body.hex()


def _hello(
    *,
    client_id: str = "c1",
    ciphers=("0x1301", "0xc02b"),
    extension_types=(0, 10, 21, 41, 43),
    groups=(0x11EC, 0x001D),
) -> dict:
    group_bytes = b"".join(g.to_bytes(2, "big") for g in groups)
    return {
        "kind": "clienthello",
        "client_id": client_id,
        "ciphers": list(ciphers),
        "extension_types": list(extension_types),
        "alpn": ["http/1.1"],
        "ext_hex": {"10": (len(group_bytes).to_bytes(2, "big") + group_bytes).hex()},
        "key_share_groups": ["0x11ec", "0x001d"],
        "raw_hex": _raw_hello(),
    }


def _request(
    *, client_id: str = "c1", url: str, request_class: str | None = "main"
) -> dict:
    headers = [["Host", "api.anthropic.com"]]
    if request_class is not None:
        headers.append(["x-claude-code-request-class", request_class])
    return {"kind": "request", "client_id": client_id, "url": url, "headers": headers}


# --------------------------------------------------------------------------
# diff_fields
# --------------------------------------------------------------------------


def test_diff_fields_splits_required_and_informational_at_nested_depth():
    candidate = {"tls": {"ciphers": ["a"], "_note": "x"}, "_cli_version": "2"}
    fixture = {"tls": {"ciphers": ["b"], "_note": "y"}, "_cli_version": "1"}

    required, informational = harness.diff_fields(candidate, fixture)

    assert required == ["tls.ciphers: fixture=['b'] candidate=['a']"]
    assert informational == [
        "_cli_version: fixture='1' candidate='2'",
        "tls._note: fixture='y' candidate='x'",
    ]


def test_diff_fields_everything_under_an_informational_key_is_informational():
    candidate = {"_platform": {"X-Stainless-OS": "MacOS", "extra": {"deep": 1}}}
    fixture = {"_platform": {"X-Stainless-OS": "Linux", "extra": {"deep": 2}}}

    required, informational = harness.diff_fields(candidate, fixture)

    assert required == []
    assert len(informational) == 2


def test_diff_fields_missing_and_new_keys():
    required, informational = harness.diff_fields(
        {"tls": {"new": 1}, "_new_info": 1}, {"tls": {"gone": 1}}
    )

    assert required == [
        "tls.gone: missing from candidate",
        "tls.new: missing from fixture (new field)",
    ]
    assert informational == ["_new_info: missing from fixture (new field)"]


def test_diff_fields_equal_is_clean():
    fp = {"tls": {"ciphers": ["a"], "_note": "x"}, "url_query": "beta=true"}
    assert harness.diff_fields(fp, json.loads(json.dumps(fp))) == ([], [])


# --------------------------------------------------------------------------
# _build_tls
# --------------------------------------------------------------------------


def test_build_tls_without_grease_excludes_conditional_extensions():
    tls = harness._build_tls(_hello())

    assert tls["grease"] is False
    assert tls["extension_order"] == [0, 10, 43]  # 21 and 41 dropped
    assert tls["compression_methods_hex"] == "0100"
    assert tls["supported_groups_ext_hex"] == "000411ec001d"


@pytest.mark.parametrize(
    "hello",
    [
        _hello(ciphers=("0x0a0a", "0x1301")),
        _hello(extension_types=(0x1A1A, 0, 10)),
        _hello(groups=(0x2A2A, 0x001D)),
    ],
    ids=["cipher", "extension", "group"],
)
def test_build_tls_detects_grease(hello):
    assert harness._build_tls(hello)["grease"] is True


def test_compression_methods_hex_skips_session_id_and_ciphers():
    raw = _raw_hello(
        session_id=bytes(range(32)),
        ciphers=(0x1301, 0x1302, 0xC02B),
        compression=b"\x01\x00",
    )
    assert harness._compression_methods_hex(bytes.fromhex(raw)) == "020100"


def test_compression_methods_hex_rejects_a_truncated_hello():
    raw = bytes.fromhex(_raw_hello(compression=b"\x00"))
    cut = raw[: raw.index(b"\x01\x00", 34) + 1]  # keep the length byte, drop the method
    with pytest.raises(harness.HarnessError):
        harness._compression_methods_hex(cut)


# --------------------------------------------------------------------------
# dist.integrity
# --------------------------------------------------------------------------


def _sri(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_integrity_accepts_matching_bytes():
    harness._verify_integrity(hashlib.sha512(b"tarball").digest(), _sri(b"tarball"))


def test_integrity_accepts_any_listed_sha512():
    integrity = f"sha1-abc {_sri(b'other')} {_sri(b'tarball')}"
    harness._verify_integrity(hashlib.sha512(b"tarball").digest(), integrity)


def test_integrity_rejects_tampered_bytes():
    with pytest.raises(harness.HarnessError, match="does not match"):
        harness._verify_integrity(
            hashlib.sha512(b"tarball-tampered").digest(), _sri(b"tarball")
        )


@pytest.mark.parametrize("integrity", ["", "sha1-abc"])
def test_integrity_requires_a_sha512_entry(integrity):
    with pytest.raises(harness.HarnessError, match="no sha512"):
        harness._verify_integrity(hashlib.sha512(b"x").digest(), integrity)


# --------------------------------------------------------------------------
# Pairing: CLI capture and --clove
# --------------------------------------------------------------------------

_MESSAGES = "https://api.anthropic.com/v1/messages?beta=true"


def test_select_main_pairs_the_main_request_with_its_own_connection():
    side_hello = _hello(client_id="side", ciphers=("0x1301",))
    main_hello = _hello(client_id="main")
    main = _request(client_id="main", url=_MESSAGES)
    records = [
        side_hello,
        _request(client_id="side", url=_MESSAGES, request_class="auxiliary"),
        main_hello,
        _request(client_id="side", url="https://api.anthropic.com/api/eval/x"),
        main,
    ]

    assert harness._select_main(records) == (main, main_hello)


def test_select_main_requires_the_hello_of_the_same_connection():
    records = [_hello(client_id="other"), _request(client_id="main", url=_MESSAGES)]
    with pytest.raises(harness.HarnessError, match="no ClientHello"):
        harness._select_main(records)


def test_select_main_requires_a_main_request():
    records = [
        _hello(),
        _request(url=_MESSAGES, request_class="auxiliary"),
        _request(url=_MESSAGES, request_class=None),
    ]
    with pytest.raises(harness.HarnessError, match="no main"):
        harness._select_main(records)


def test_select_clove_pairs_the_plain_request_with_the_last_hello():
    first, last = _hello(client_id="t1"), _hello(client_id="t2")
    plain = _request(client_id="p", url=harness._CLOVE_PLAIN_URL)
    records = [
        first,
        last,
        _request(client_id="t2", url=harness._CLOVE_TLS_URL),
        _request(client_id="p", url="http://api.anthropic.com/v1/models"),
        plain,
    ]

    assert harness._select_clove(records) == (plain, last)


def test_select_clove_requires_a_hello():
    with pytest.raises(harness.HarnessError, match="no ClientHello"):
        harness._select_clove([_request(url=harness._CLOVE_PLAIN_URL)])


def test_select_clove_requires_the_plain_request():
    records = [_hello(), _request(url=harness._CLOVE_TLS_URL)]
    with pytest.raises(harness.HarnessError, match="plain-http"):
        harness._select_clove(records)


# --------------------------------------------------------------------------
# --version fixture and exit codes
# --------------------------------------------------------------------------


@pytest.fixture
def fixture_file(tmp_path):
    path = tmp_path / "fingerprint.json"
    path.write_text(
        json.dumps({"_cli_version": "2.1.283", "url_query": "beta=true", "_x": 1})
    )
    return path


def test_version_fixture_resolves_to_the_fixture_cli_version(fixture_file):
    assert harness._resolve_version("fixture", fixture_file) == "2.1.283"


def test_version_fixture_rejects_a_fixture_without_a_version(tmp_path):
    path = tmp_path / "fp.json"
    path.write_text("{}")
    with pytest.raises(harness.HarnessError, match="_cli_version"):
        harness._resolve_version("fixture", path)


def _capture_returning(fingerprint: dict):
    def capture(version, timeout, fixture_path):
        return fingerprint, b"{}", "2.1.283"

    return capture


def _capture_raising(exc: Exception):
    def capture(*args, **kwargs):
        raise exc

    return capture


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (harness.HarnessUnavailable("no U1"), harness.EXIT_UNAVAILABLE),
        (harness.CaptureDrift("no billing block"), harness.EXIT_DRIFT),
        (harness.HarnessError("download failed"), harness.EXIT_HARNESS_ERROR),
        (KeyError("dist"), harness.EXIT_HARNESS_ERROR),
    ],
)
@pytest.mark.parametrize("mode", ["--check", "--clove"])
def test_exceptions_map_to_exit_codes(monkeypatch, fixture_file, mode, exc, code):
    monkeypatch.setattr(harness, "capture_via_cli", _capture_raising(exc))
    monkeypatch.setattr(harness, "capture_via_clove", _capture_raising(exc))
    assert harness.main([mode, "--fixture", str(fixture_file)]) == code


@pytest.mark.parametrize(
    ("fingerprint", "billing_ok", "code"),
    [
        ({"url_query": "beta=true", "_x": 2}, True, harness.EXIT_OK),
        ({"url_query": "beta=false", "_x": 1}, True, harness.EXIT_DRIFT),
        ({"url_query": "beta=true", "_x": 1}, False, harness.EXIT_DRIFT),
    ],
    ids=["informational-only", "required-drift", "billing-failure"],
)
def test_check_results_map_to_exit_codes(
    monkeypatch, fixture_file, fingerprint, billing_ok, code
):
    fingerprint = {"_cli_version": "2.1.283", **fingerprint}
    monkeypatch.setattr(harness, "capture_via_cli", _capture_returning(fingerprint))
    monkeypatch.setattr(harness, "_billing_gate", lambda raw: ("stub", billing_ok))
    assert harness.main(["--check", "--fixture", str(fixture_file)]) == code


def test_missing_fixture_is_a_harness_error(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "capture_via_cli", _capture_returning({}))
    missing = tmp_path / "missing.json"
    assert (
        harness.main(["--check", "--fixture", str(missing)])
        == harness.EXIT_HARNESS_ERROR
    )


@pytest.mark.parametrize(
    "body",
    [
        {"system": "one string", "metadata": {"user_id": "{}"}},
        {"system": [{"text": "not billing"}, {"text": "x"}]},
    ],
    ids=["system-not-a-list", "no-billing-prefix"],
)
def test_a_changed_request_shape_is_drift_not_a_harness_error(body):
    with pytest.raises(harness.CaptureDrift):
        harness._parse_system_blocks(body)


# --------------------------------------------------------------------------
# --clove's two requests
# --------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, tls_error: Exception | None):
        self.tls_error = tls_error
        self.calls: list[tuple[str, str, object, bytes]] = []
        self.closed = False

    async def request(self, method, url, *, headers, data=None):
        self.calls.append((method, url, headers, data))
        if url == harness._CLOVE_TLS_URL and self.tls_error is not None:
            raise self.tls_error
        return object()

    async def close(self):
        self.closed = True


_HEADERS = [("Accept", "application/json"), ("Connection", "keep-alive")]


async def test_clove_sends_the_same_request_over_plain_http_after_the_tls_failure():
    tls_error = ConnectionError("certificate verify failed")
    session = _FakeSession(tls_error)

    returned = await harness._send_clove_requests(
        lambda proxy: session, "http://127.0.0.1:1", _HEADERS, b"{}"
    )

    assert returned is tls_error
    assert session.calls == [
        ("POST", harness._CLOVE_TLS_URL, _HEADERS, b"{}"),
        ("POST", harness._CLOVE_PLAIN_URL, _HEADERS, b"{}"),
    ]
    assert session.closed


async def test_clove_rejects_a_transport_that_trusts_the_mitm_certificate():
    session = _FakeSession(tls_error=None)

    with pytest.raises(harness.CaptureDrift, match="untrusted"):
        await harness._send_clove_requests(
            lambda proxy: session, "http://127.0.0.1:1", _HEADERS, b"{}"
        )

    assert [url for _, url, _, _ in session.calls] == [harness._CLOVE_TLS_URL]
    assert session.closed


async def test_clove_without_rnet_is_unavailable():
    def factory(proxy):
        raise RuntimeError("rnet is not installed")

    with pytest.raises(harness.HarnessUnavailable, match="rnet"):
        await harness._send_clove_requests(factory, "http://127.0.0.1:1", [], b"")
