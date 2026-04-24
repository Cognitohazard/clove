# Clove (Cognitohazard Fork)

Fork of [Huan-zhaojun/clove](https://github.com/Huan-zhaojun/clove), which is itself a fork of [mirrorange/clove](https://github.com/mirrorange/clove).

This fork tries to keep the OAuth API path as close to a transparent Anthropic proxy as practical: preserve the original Messages API request body, pass through client API fields and beta headers, and avoid proxy-side behavior that can change upstream semantics.

For base project documentation (features, limitations, API usage, configuration), see the upstream READMEs.

## Quick Start

```bash
docker run -d --name clove --restart unless-stopped \
  -p 5201:5201 -v ./data:/data \
  -e HOST=0.0.0.0 -e PORT=5201 -e DATA_FOLDER=/data \
  -e LOG_LEVEL=INFO -e LOG_TO_FILE=true -e LOG_FILE_PATH=/data/logs/app.log \
  ghcr.io/cognitohazard/clove:latest
```

Or with Docker Compose (uses `ghcr.io/cognitohazard/clove:latest`):

```bash
mkdir -p clove && cd clove
# download docker-compose.yml from this repo
docker compose up -d
```

## What This Fork Adds (vs Huan-zhaojun/clove)

### Transparent Models Proxy

`/v1/models` and `/v1/models/{model_id}` endpoints that proxy directly to the Anthropic API through authenticated OAuth sessions. Clients can discover available models without hardcoding.

**Where:** `app/api/routes/models.py`

### Claude Code Prompt Toggle

Configurable `inject_claude_code_system_prompt` setting (default: `true`) to control whether the legacy "You are Claude Code" system prompt is injected into API requests. Allows disabling it for non-Claude-Code clients.

**Where:** `app/core/config.py`, `app/processors/claude_ai/claude_api_processor.py`

### Messages API Single-Attempt Proxying

`POST /v1/messages` now makes exactly one upstream Messages API attempt on the OAuth path. The route-level retry wrapper was removed, and the OAuth Messages session overrides HTTP transport retries with `request_retries=1`. This avoids duplicate upstream generations when a streamed request fails after Anthropic has already accepted it.

Shared HTTP sessions still support configurable transport retries through `REQUEST_RETRIES` and `REQUEST_RETRY_INTERVAL`, so other callers can keep retry behavior without forcing it onto Messages API proxying.

**Where:** `app/api/routes/claude.py`, `app/core/http_client.py`, `app/processors/claude_ai/claude_api_processor.py`

### OAuth Beta Header Passthrough

OAuth Messages and Models proxy requests inject the required `oauth-2025-04-20` beta header and merge any client-provided `anthropic-beta` values without duplicating entries. Optional betas such as `context-1m-2025-08-07` should be supplied by the client when needed instead of being forced globally by the proxy.

**Where:** `app/processors/claude_ai/claude_api_processor.py`, `app/api/routes/models.py`

### 1-Hour Cache TTL

Cache service recognizes `1h` as a TTL value (resolves to 3600 seconds), in addition to existing TTL options.

**Where:** `app/services/cache.py`

### OAuth Resilience Fixes

Three fixes to prevent OAuth token loss and unnecessary retries:

- **Transient refresh failure protection:** Exponential backoff (60s/120s/240s, max 3 retries) before treating a refresh failure as permanent. Prevents transient network errors from wiping valid tokens. (`app/services/oauth.py`)
- **429 retry guard:** Stops aggressive retry stacking on OAuth token endpoint 429 responses. (`app/services/oauth.py`)
- **Plain HTTP client for token exchange:** Dedicated `create_plain_session()` and `_token_request()` that use a non-impersonating HTTP client (prefers httpx) with form-encoded data and `claude-cli` User-Agent for `console.anthropic.com` OAuth endpoints. Prevents 429s caused by browser TLS fingerprints. Based on upstream mirrorange/clove@156efcd. (`app/core/http_client.py`, `app/services/oauth.py`)

### Structured Request Observability

Per-request `RequestSpan` emitted as one JSON line to a dedicated `access.log` (disabled by default). Covers model, upstream (oauth/web), account, client key, HTTP status, duration, token usage and cache hits — all sensitive fields masked. A loguru patcher injects `request_id` into every other log line so the stdout/app log can be grepped by request. Status taxonomy, sampling, and rotation are all configurable; `x-request-id` is echoed on every traced response including 500s. Pure ASGI middleware, composable `SpanExporter` Protocol (`Loguru` / `Sampled` / `Multi` / `Null`), 62 pytest tests.

Enable with `ACCESS_LOG_ENABLED=true`. See [docs/observability.md](docs/observability.md) for configuration, log format, and jq queries.

**Where:** `app/core/observability/`, `app/utils/logger.py`, `tests/test_{span,exporter,middleware,usage_tap}.py`

### CI/CD & Infrastructure

- **Auto-merge upstream workflow:** Daily (08:00 UTC) automatic merge from Huan-zhaojun/clove, with frontend submodule sync and conflict issue creation on failure. (`.github/workflows/auto-merge-upstream.yml`)
- **Fork GHCR image:** Docker image published to `ghcr.io/cognitohazard/clove` instead of upstream's registry.
- **Frontend submodule repointed** to `Cognitohazard/clove-front`.
- **PyPI publish workflow removed** (this fork is Docker-only).

## What Huan-zhaojun/clove Adds (vs mirrorange/clove)

### Dynamic Proxy Pool

Full proxy management system with three modes (disabled/fixed/dynamic) and four rotation strategies (sequential/random/round-robin/least-connections). Includes health checking and automatic failover.

**Where:** `app/services/proxy.py` (796 lines, entirely new), `app/models/proxy.py`, `app/api/routes/proxies.py`

### Multi-Account Management Enhancements

- Search, filter, sort, and paginate the account list
- Batch operations (add cookies, delete, refresh status)
- Account status refresh with credential validation and rate-limit probing
- Dashboard account count card with status breakdown
- Concurrent cookie processing for bulk adds

**Where:** `app/services/account.py` (+414 lines), `app/api/routes/accounts.py`

### Web Search Support

Native web search support through the Claude Web link, enabling search-augmented responses via the web proxy path.

**Where:** `app/processors/claude_ai/claude_web_processor.py`

### Extended Thinking for Free Accounts

Removed the `is_pro` gate so Free-tier accounts can use extended thinking (chain-of-thought).

**Where:** `app/processors/claude_ai/claude_api_processor.py`

### Transparent OAuth Proxy Passthrough

OAuth mode now forces transparent passthrough for Messages API requests: the processor forwards the original raw request body to Anthropic instead of rebuilding it from Pydantic models. This fixes cache-scope requests from upstream because fields such as `cache_control.scope` are preserved exactly, even though the local cache model only needs to inspect `type` and `ttl`.

The upstream round-trip happens on the OAuth path: FastAPI parses the request into `MessagesAPIRequest`, then `ClaudeAPIProcessor` sends `context.messages_api_request.model_dump_json(exclude_none=True)`. Because upstream `CacheControl` does not allow extra fields, `scope` can be dropped during parsing/serialization before the request reaches Anthropic. This fork avoids that path by forwarding the raw body, and Pydantic models also inherit a project `BaseModel` with `extra="allow"` so parsed API data keeps unknown fields where local processing still needs models.

**Where:** `app/models/claude.py` (BaseModel), `app/processors/claude_ai/claude_api_processor.py`

### Claude API Spec Alignment

Updated thinking/effort/beta headers and stop reasons to match the latest Claude API specification. Adds `model_context_window_exceeded` stop reason (Claude 4.6), explicit `Tool.type` field for server tools, and updated default model to `claude-sonnet-4-6`.

**Where:** `app/models/claude.py`, `app/models/streaming.py`, `app/processors/claude_ai/claude_api_processor.py`

### Trivy CI Fix

Cached Trivy DB with multi-source fallback (ECR Public / ghcr.io) to avoid intermittent `mirror.gcr.io` 404 failures.

**Where:** `.github/workflows/docker-publish.yml`

### Web Proxy Robustness

- Image uploads use per-conversation wiggle endpoints; upload failures abort immediately
- File count over-limit is caught client-side before sending
- `invalid_request_error` responses are not retried
- Pure-image requests (no text) are supported
- Removed hardcoded system prompt injection that caused 400 errors

### Docker & Build

- Migrated from pip to uv in Dockerfile
- Added Asia/Shanghai timezone config
- Enabled local `docker compose up --build` alongside remote image pull

### Other

- Cookie validation compatible with `sk-ant-sid02` and later formats
- `refusal` and `pause_turn` stop reasons handled in streaming
- i18n locale updates
- CLAUDE.md, AGENTS.md, and extensive documentation (`docs/`)

## License

MIT - see [LICENSE](LICENSE).
