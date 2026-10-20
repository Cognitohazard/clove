"""HTTP client abstraction layer that supports both curl_cffi and httpx."""

import functools
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Optional, Dict, Any, Tuple, AsyncIterator
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)
from loguru import logger
import json

from app.core.config import settings
from app.utils.retry import log_before_sleep

try:
    import rnet
    from rnet import Client as RnetClient, Method as RnetMethod
    from rnet.exceptions import RequestError as RnetRequestError

    RNET_AVAILABLE = True
except ImportError:
    RNET_AVAILABLE = False

try:
    from curl_cffi.requests import (
        AsyncSession as CurlAsyncSession,
        Response as CurlResponse,
    )
    from curl_cffi.requests.exceptions import RequestException as CurlRequestException
    import curl_cffi

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# Always try to import httpx as fallback
try:
    import httpx

    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

if not RNET_AVAILABLE and not CURL_CFFI_AVAILABLE and not HTTPX_AVAILABLE:
    raise ImportError(
        "Neither rnet, curl_cffi nor httpx is installed. Please install at least one of them."
    )


async def _run_with_retries(
    operation: Callable[[], Awaitable[Any]],
    retry_exception: type[BaseException] | tuple[type[BaseException], ...],
    attempts: int,
) -> Any:
    if attempts <= 1:
        return await operation()

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(attempts),
        wait=wait_fixed(settings.request_retry_interval),
        retry=retry_if_exception_type(retry_exception),
        before_sleep=log_before_sleep,
        reraise=True,
    ):
        with attempt:
            return await operation()

    raise RuntimeError("Retry loop exited without returning or raising")


class Response(ABC):
    """Abstract response class."""

    @property
    @abstractmethod
    def status_code(self) -> int:
        """Get response status code."""
        pass

    @abstractmethod
    async def json(self) -> Any:
        """Parse response as JSON."""
        pass

    @property
    @abstractmethod
    def headers(self) -> Dict[str, str]:
        """Get response headers."""
        pass

    @abstractmethod
    def aiter_bytes(self, chunk_size: Optional[int] = None) -> AsyncIterator[bytes]:
        """Iterate over response bytes."""
        pass


class CurlResponseWrapper(Response):
    """curl_cffi response wrapper."""

    def __init__(self, response: "CurlResponse", stream: bool = False):
        self._response = response
        self._stream = stream

    @property
    def status_code(self) -> int:
        return self._response.status_code

    async def json(self) -> Any:
        if self._stream:
            content = ""
            async for chunk in self._response.aiter_content():
                content += chunk.decode("utf-8")
            return json.loads(content)
        else:
            return self._response.json()

    @property
    def headers(self) -> Dict[str, str]:
        return self._response.headers

    async def aiter_bytes(
        self, chunk_size: Optional[int] = None
    ) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_content(chunk_size):
            yield chunk
        await self._response.aclose()


class HttpxResponse(Response):
    """httpx response wrapper."""

    def __init__(self, response: httpx.Response):
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    async def json(self) -> Any:
        await self._response.aread()
        return self._response.json()

    @property
    def headers(self) -> Dict[str, str]:
        return self._response.headers

    async def aiter_bytes(
        self, chunk_size: Optional[int] = None
    ) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes(chunk_size):
            yield chunk
        await self._response.aclose()


if RNET_AVAILABLE:

    class RnetResponse(Response):
        """rnet response wrapper."""

        def __init__(self, response: "rnet.Response"):
            self._response = response

        @property
        def status_code(self) -> int:
            return self._response.status.as_int()

        async def json(self) -> Any:
            return await self._response.json()

        @property
        def headers(self) -> Dict[str, str]:
            headers_dict = {}
            for key, value in self._response.headers:
                key_str = key.decode("utf-8") if isinstance(key, bytes) else key
                value_str = value.decode("utf-8") if isinstance(value, bytes) else value
                headers_dict[key_str] = value_str
            return headers_dict

        async def aiter_bytes(
            self, chunk_size: Optional[int] = None
        ) -> AsyncIterator[bytes]:
            async with self._response.stream() as streamer:
                async for chunk in streamer:
                    yield chunk
            await self._response.close()


class AsyncSession(ABC):
    """Abstract async session class."""

    @abstractmethod
    async def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        stream: bool = False,
        **kwargs,
    ) -> Response:
        """Make an HTTP request."""
        pass

    @abstractmethod
    async def close(self):
        """Close the session."""
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


if CURL_CFFI_AVAILABLE:

    class CurlAsyncSessionWrapper(AsyncSession):
        """curl_cffi async session wrapper."""

        def __init__(
            self,
            timeout: int = settings.request_timeout,
            impersonate: Optional[str] = "chrome",
            proxy: Optional[str] = settings.proxy_url,
            follow_redirects: bool = True,
            request_retries: int = settings.request_retries,
        ):
            self._request_retries = request_retries
            self._session = CurlAsyncSession(
                timeout=timeout,
                impersonate=impersonate or None,
                proxy=proxy,
                allow_redirects=follow_redirects,
            )

        def process_files(self, files: dict) -> curl_cffi.CurlMime:
            # Create multipart form
            multipart = curl_cffi.CurlMime()

            # Handle different file formats
            if isinstance(files, dict):
                for field_name, file_info in files.items():
                    if isinstance(file_info, tuple):
                        # Format: {"field": (filename, data, content_type)}
                        if len(file_info) >= 3:
                            filename, file_data, content_type = file_info[:3]
                        elif len(file_info) == 2:
                            filename, file_data = file_info
                            content_type = "application/octet-stream"
                        else:
                            raise ValueError(
                                f"Invalid file tuple format for field {field_name}"
                            )

                        multipart.addpart(
                            name=field_name,
                            content_type=content_type,
                            filename=filename,
                            data=file_data,
                        )
                    else:
                        # Simple format: {"field": data}
                        multipart.addpart(
                            name=field_name,
                            data=file_info,
                        )

            return multipart

        async def request(
            self,
            method: str,
            url: str,
            headers: Optional[Dict[str, str]] = None,
            json: Optional[Any] = None,
            data: Optional[Any] = None,
            stream: bool = False,
            **kwargs,
        ) -> Response:
            logger.debug(f"Making {method} request to {url}")

            # Handle file uploads - convert files parameter to multipart
            files = kwargs.pop("files", None)

            async def operation() -> Response:
                request_kwargs = kwargs.copy()
                multipart = None

                if files:
                    multipart = self.process_files(files)
                    request_kwargs["multipart"] = multipart

                try:
                    response = await self._session.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json,
                        data=data,
                        stream=stream,
                        **request_kwargs,
                    )
                    return CurlResponseWrapper(response, stream=stream)
                finally:
                    if multipart:
                        multipart.close()

            return await _run_with_retries(
                operation, CurlRequestException, self._request_retries
            )

        async def close(self):
            await self._session.close()


if RNET_AVAILABLE:

    class RnetAsyncSession(AsyncSession):
        """rnet async session wrapper."""

        def __init__(
            self,
            timeout: int = settings.request_timeout,
            impersonate: Optional[str] = "chrome",
            proxy: Optional[str] = settings.proxy_url,
            follow_redirects: bool = True,
            request_retries: int = settings.request_retries,
        ):
            self._request_retries = request_retries
            # Map impersonate string to rnet Emulation enum
            emulation_map = {
                "chrome": rnet.Emulation.Chrome142,
                "firefox": rnet.Emulation.Firefox136,
                "safari": rnet.Emulation.Safari18,
                "edge": rnet.Emulation.Edge134,
            }

            # Use Chrome as default; empty/None disables impersonation
            if impersonate:
                rnet_emulation = emulation_map.get(
                    impersonate.lower(), rnet.Emulation.Chrome142
                )
            else:
                rnet_emulation = None

            # Create proxy list if proxy is provided
            proxies = None
            if proxy:
                proxies = [rnet.Proxy.all(proxy)]

            self._client = RnetClient(
                emulation=rnet_emulation,
                timeout=timeout,
                proxies=proxies,
                allow_redirects=follow_redirects,
            )

        async def request(
            self,
            method: str,
            url: str,
            headers: Optional[Dict[str, str]] = None,
            json: Optional[Any] = None,
            data: Optional[Any] = None,
            stream: bool = False,
            **kwargs,
        ) -> Response:
            logger.debug(f"Making {method} request to {url}")
            files = kwargs.pop("files", None)

            async def operation() -> Response:
                request_kwargs = kwargs.copy()

                # Map method string to rnet Method enum
                method_map = {
                    "GET": RnetMethod.GET,
                    "POST": RnetMethod.POST,
                    "PUT": RnetMethod.PUT,
                    "DELETE": RnetMethod.DELETE,
                    "PATCH": RnetMethod.PATCH,
                    "HEAD": RnetMethod.HEAD,
                    "OPTIONS": RnetMethod.OPTIONS,
                    "TRACE": RnetMethod.TRACE,
                }

                rnet_method = method_map.get(method.upper(), RnetMethod.GET)

                # Handle file uploads - convert files parameter to multipart
                multipart = None

                if files:
                    # Convert files dict to rnet Multipart
                    parts = []
                    for field_name, file_info in files.items():
                        if isinstance(file_info, tuple):
                            # Format: {"field": (filename, data, content_type)}
                            if len(file_info) >= 3:
                                filename, file_data, content_type = file_info[:3]
                            elif len(file_info) == 2:
                                filename, file_data = file_info
                                content_type = "application/octet-stream"
                            else:
                                raise ValueError(
                                    f"Invalid file tuple format for field {field_name}"
                                )

                            parts.append(
                                rnet.Part(
                                    name=field_name,
                                    value=file_data,
                                    filename=filename,
                                    mime=content_type,
                                )
                            )
                        else:
                            # Simple format: {"field": data}
                            parts.append(rnet.Part(name=field_name, value=file_info))

                    multipart = rnet.Multipart(*parts)
                    request_kwargs["multipart"] = multipart

                if headers:
                    request_kwargs["headers"] = headers
                if json is not None:
                    request_kwargs["json"] = json
                elif data is not None:
                    # rnet uses 'form' for form data, 'body' for raw data
                    if isinstance(data, dict) or isinstance(data, list):
                        request_kwargs["form"] = (
                            [(k, v) for k, v in data.items()]
                            if isinstance(data, dict)
                            else data
                        )
                    else:
                        request_kwargs["body"] = data

                response = await self._client.request(
                    method=rnet_method,
                    url=url,
                    **request_kwargs,
                )

                return RnetResponse(response)

            return await _run_with_retries(
                operation, RnetRequestError, self._request_retries
            )

        async def close(self):
            # rnet Client doesn't have an explicit close method
            # The connection pooling is handled internally
            pass


if HTTPX_AVAILABLE:

    class HttpxAsyncSession(AsyncSession):
        """httpx async session wrapper."""

        def __init__(
            self,
            timeout: int = settings.request_timeout,
            impersonate: Optional[str] = "chrome",
            proxy: Optional[str] = settings.proxy_url,
            follow_redirects: bool = True,
            request_retries: int = settings.request_retries,
        ):
            self._request_retries = request_retries
            self._client = httpx.AsyncClient(
                timeout=timeout,
                proxy=proxy,
                follow_redirects=follow_redirects,
            )

        async def stream(
            self,
            method: str,
            url: str,
            headers: Optional[Dict[str, str]] = None,
            json: Optional[Any] = None,
            data: Optional[Any] = None,
            **kwargs,
        ) -> Response:
            """
            Alternative to `httpx.request()` that streams the response body
            instead of loading it into memory at once.

            **Parameters**: See `httpx.request`.

            See also: [Streaming Responses][0]

            [0]: /quickstart#streaming-responses
            """
            request = self._client.build_request(
                method=method,
                url=url,
                data=data,
                json=json,
                headers=headers,
                **kwargs,
            )
            response = await self._client.send(
                request=request,
                stream=True,
            )

            return response

        async def request(
            self,
            method: str,
            url: str,
            headers: Optional[Dict[str, str]] = None,
            json: Optional[Any] = None,
            data: Optional[Any] = None,
            stream: bool = False,
            **kwargs,
        ) -> Response:
            logger.debug(f"Making {method} request to {url}")

            async def operation() -> Response:
                if stream:
                    response = await self.stream(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json,
                        data=data,
                        **kwargs,
                    )
                else:
                    response = await self._client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json,
                        data=data,
                        **kwargs,
                    )

                return HttpxResponse(response)

            return await _run_with_retries(
                operation, httpx.RequestError, self._request_retries
            )

        async def close(self):
            await self._client.aclose()


def create_session(
    timeout: int = settings.request_timeout,
    impersonate: Optional[str] = "chrome",
    proxy: Optional[str] = settings.proxy_url,
    follow_redirects: bool = True,
    request_retries: int = settings.request_retries,
) -> AsyncSession:
    """Create an async session using the available HTTP client.

    Prefers rnet if available, then curl_cffi, falls back to httpx.
    """
    if RNET_AVAILABLE:
        logger.debug("Using rnet as HTTP client")
        return RnetAsyncSession(
            timeout=timeout,
            impersonate=impersonate,
            proxy=proxy,
            follow_redirects=follow_redirects,
            request_retries=request_retries,
        )
    elif CURL_CFFI_AVAILABLE:
        logger.debug("Using curl_cffi as HTTP client")
        return CurlAsyncSessionWrapper(
            timeout=timeout,
            impersonate=impersonate,
            proxy=proxy,
            follow_redirects=follow_redirects,
            request_retries=request_retries,
        )
    else:
        logger.debug("Using httpx as HTTP client (rnet and curl_cffi not available)")
        return HttpxAsyncSession(
            timeout=timeout,
            impersonate=impersonate,
            proxy=proxy,
            follow_redirects=follow_redirects,
            request_retries=request_retries,
        )


def create_plain_session(
    timeout: int = settings.request_timeout,
    proxy: Optional[str] = settings.proxy_url,
    follow_redirects: bool = True,
) -> AsyncSession:
    """Create a plain HTTP session WITHOUT browser fingerprinting/impersonation.

    Used for standard API endpoints (e.g. console.anthropic.com OAuth token exchange)
    that reject browser-like TLS fingerprints.

    Intentionally prefers httpx first (reversed from create_session's rnet-first order)
    because httpx produces zero TLS/header fingerprint artifacts.
    """
    if HTTPX_AVAILABLE:
        return HttpxAsyncSession(
            timeout=timeout,
            proxy=proxy,
            follow_redirects=follow_redirects,
        )
    elif CURL_CFFI_AVAILABLE:
        return CurlAsyncSessionWrapper(
            timeout=timeout,
            impersonate=None,
            proxy=proxy,
            follow_redirects=follow_redirects,
        )
    else:
        return RnetAsyncSession(
            timeout=timeout,
            impersonate=None,
            proxy=proxy,
            follow_redirects=follow_redirects,
        )


async def download_image(url: str, timeout: int = 30) -> Tuple[bytes, str]:
    """Download an image from a URL and return content and content type.

    Uses the unified session interface that works with both curl_cffi and httpx.
    """
    async with create_session(timeout=timeout) as session:
        response = await session.request("GET", url)
        content_type = response.headers.get("content-type", "image/jpeg")

        # Read the response content
        content = b""
        async for chunk in response.aiter_bytes():
            content += chunk

        return content, content_type


# Export the appropriate exception class
if RNET_AVAILABLE:
    RequestException = RnetRequestError
elif CURL_CFFI_AVAILABLE:
    RequestException = CurlRequestException
else:
    RequestException = httpx.RequestError


# Export proxy-related network exceptions for health marking
# These indicate proxy issues that should trigger rotation
if RNET_AVAILABLE:
    from rnet.exceptions import (
        ConnectionError as RnetConnectionError,
        ConnectionResetError as RnetConnectionResetError,
        TimeoutError as RnetTimeoutError,
        TlsError as RnetTlsError,
    )

    # 精确分类：只包含代理相关的网络异常
    # 排除：DNSResolverError（本地DNS）、RedirectError（目标服务器）、
    #      DecodingError/BodyError/BuilderError/URLParseError（代码/数据问题）
    ProxyNetworkException = (
        RnetConnectionError,  # 代理连接失败
        RnetConnectionResetError,  # 连接被重置
        RnetTimeoutError,  # 连接超时
        RnetTlsError,  # TLS 握手失败
        RnetRequestError,  # 通用请求错误
    )
elif CURL_CFFI_AVAILABLE:
    ProxyNetworkException = (CurlRequestException,)
else:
    ProxyNetworkException = (httpx.RequestError,)


# --------------------------------------------------------------------------
# OAuth transport: the Claude Code CLI's TLS and HTTP/1.1 request shape
# --------------------------------------------------------------------------

# OAuth transport failures that mean the proxy, or the path through it, is bad:
# callers mark the proxy unhealthy. TimeoutError is left out on purpose: the
# request timeout is a total one, so a slow generation would mark a healthy proxy.
if RNET_AVAILABLE:
    OAUTH_TRANSPORT_ERRORS: tuple[type[Exception], ...] = (
        RnetRequestError,
        RnetConnectionError,
        RnetConnectionResetError,
        RnetTlsError,
    )
else:
    OAUTH_TRANSPORT_ERRORS = ()  # create_oauth_session raises without rnet

# BoringSSL names in the CLI's ClientHello order (``TLS_PROFILE`` in
# app/utils/claude_code_identity.py): 3 TLS 1.3 suites, then 14 TLS 1.2 ones.
_CLI_CIPHERS = ":".join(
    [
        "TLS_AES_128_GCM_SHA256",
        "TLS_AES_256_GCM_SHA384",
        "TLS_CHACHA20_POLY1305_SHA256",
        "ECDHE-ECDSA-AES128-GCM-SHA256",
        "ECDHE-RSA-AES128-GCM-SHA256",
        "ECDHE-ECDSA-AES256-GCM-SHA384",
        "ECDHE-RSA-AES256-GCM-SHA384",
        "ECDHE-ECDSA-CHACHA20-POLY1305",
        "ECDHE-RSA-CHACHA20-POLY1305",
        "ECDHE-ECDSA-AES128-SHA",
        "ECDHE-RSA-AES128-SHA",
        "ECDHE-ECDSA-AES256-SHA",
        "ECDHE-RSA-AES256-SHA",
        "AES128-GCM-SHA256",
        "AES256-GCM-SHA384",
        "AES128-SHA",
        "AES256-SHA",
    ]
)
_CLI_CURVES = "X25519MLKEM768:X25519:P-256:P-384"
_CLI_SIGALGS = ":".join(
    [
        "ecdsa_secp256r1_sha256",
        "rsa_pss_rsae_sha256",
        "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384",
        "rsa_pss_rsae_sha384",
        "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512",
        "rsa_pkcs1_sha512",
        "ed25519",
        "ecdsa_secp521r1_sha512",
        "rsa_pkcs1_sha1",
    ]
)
# Below common server keep-alive timeouts (60 s and up), so a pooled connection
# is rarely one the server has already closed.
_OAUTH_POOL_IDLE_TIMEOUT = 30


def _wire_header_order(names: Sequence[str], has_body: bool) -> list[str]:
    """rnet ``orig_headers``: ``names`` in order plus the two headers rnet computes.

    ``Host`` goes right after ``Connection`` (appended when there is none) and
    ``Content-Length`` last when a body is sent, which is where the CLI puts them.
    """
    order = list(names)
    lowered = [name.lower() for name in order]
    host_at = lowered.index("connection") + 1 if "connection" in lowered else len(order)
    order.insert(host_at, "Host")
    if has_body:
        order.append("Content-Length")
    return order


if RNET_AVAILABLE:

    @functools.lru_cache(maxsize=64)
    def _oauth_client(proxy: Optional[str]) -> "RnetClient":
        """One client per proxy URL, so connections and TLS session tickets are
        reused like the CLI's but never cross proxies."""
        return RnetClient(
            tls_options=rnet.TlsOptions(
                cipher_list=_CLI_CIPHERS,
                preserve_tls13_cipher_list=True,  # keep AES first on non-AES CPUs
                curves_list=_CLI_CURVES,
                sigalgs_list=_CLI_SIGALGS,
                alpn_protocols=[rnet.AlpnProtocol.HTTP1],
                grease_enabled=False,
                permute_extensions=False,
                enable_ech_grease=False,
                session_ticket=True,
                psk_dhe_ke=True,
                pre_shared_key=False,
                enable_ocsp_stapling=True,
                enable_signed_cert_timestamps=True,
                renegotiation=True,
                min_tls_version=rnet.TlsVersion.TLS_1_2,
                max_tls_version=rnet.TlsVersion.TLS_1_3,
                key_shares_limit=2,
            ),
            http1_only=True,
            proxies=[rnet.Proxy.all(proxy)] if proxy else None,
            allow_redirects=False,
            pool_idle_timeout=_OAUTH_POOL_IDLE_TIMEOUT,
            gzip=True,
            brotli=True,
            deflate=True,
            zstd=True,
        )

    def _header_map(pairs: Sequence[tuple[str, str]], retries: int) -> "rnet.HeaderMap":
        """``pairs`` as given, except ``X-Stainless-Retry-Count`` on a retry."""
        header_map = rnet.HeaderMap()
        for name, value in pairs:
            if retries and name.lower() == "x-stainless-retry-count":
                value = str(retries)
            header_map.insert(name, value)
        return header_map

    class OAuthSession(AsyncSession):
        """Sends requests with the Claude Code CLI's TLS, HTTP/1.1 and header shape.

        Headers go out exactly as given, in order and casing; rnet adds none of
        its own except ``Host`` and ``Content-Length`` (see ``_wire_header_order``).
        Responses are decoded. The client is shared per proxy, so ``close()`` is a
        no-op. A dead pooled connection surfaces as a ``RequestError``
        (connection closed) or a ``ConnectionResetError``; both are retried once.
        """

        def __init__(self, client: "RnetClient", timeout: Optional[int] = None):
            self._client = client
            self._timeout = timeout

        async def request(
            self,
            method: str,
            url: str,
            *,
            headers: Mapping[str, str] | Sequence[tuple[str, str]],
            data: Optional[bytes] = None,
            stream: bool = False,  # rnet responses always stream
            params: Optional[Mapping[str, str]] = None,
            timeout: Optional[int] = None,
        ) -> Response:
            """``timeout``: this call's, else the session's, else ``REQUEST_TIMEOUT`` now."""
            pairs = list(headers.items() if isinstance(headers, Mapping) else headers)
            kwargs: Dict[str, Any] = {
                "orig_headers": _wire_header_order(
                    [name for name, _ in pairs], data is not None
                ),
                "default_headers": False,
                "timeout": timeout or self._timeout or settings.request_timeout,
            }
            if data is not None:
                kwargs["body"] = data
            if params:
                kwargs["query"] = dict(params)
            rnet_method = getattr(RnetMethod, method.upper())
            attempts = 0

            async def operation() -> Response:
                nonlocal attempts
                header_map = _header_map(pairs, retries=attempts)
                attempts += 1
                return RnetResponse(
                    await self._client.request(
                        rnet_method, url, headers=header_map, **kwargs
                    )
                )

            # ponytail: one retry. rnet cannot tell a stale pooled connection from
            # a fresh one that dropped after the request went out, so a
            # non-idempotent POST may reach upstream twice. The Stainless SDK the
            # CLI is built on retries the same way and counts it in
            # X-Stainless-Retry-Count, which the retried attempt sends as 1.
            return await _run_with_retries(
                operation, (RnetRequestError, RnetConnectionResetError), 2
            )

        async def close(self):
            pass


def create_oauth_session(
    proxy: Optional[str] = None, *, timeout: Optional[int] = None
) -> AsyncSession:
    """Session for every OAuth call to ``CLAUDE_API_BASEURL``.

    Always rnet, the only client here that can reproduce the CLI's ClientHello.
    ``timeout`` is the per-request default (``None``: ``REQUEST_TIMEOUT`` at call
    time); ``request(timeout=...)`` overrides it.
    """
    if not RNET_AVAILABLE:
        raise RuntimeError("The OAuth transport requires rnet (the 'rnet' extra)")
    return OAuthSession(_oauth_client(proxy), timeout)
