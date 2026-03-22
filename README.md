# Clove (Cognitohazard Fork)

Fork of [Huan-zhaojun/clove](https://github.com/Huan-zhaojun/clove), which is itself a fork of [mirrorange/clove](https://github.com/mirrorange/clove).

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

### 1-Hour Cache TTL

Cache service recognizes `1h` as a TTL value (resolves to 3600 seconds), in addition to existing TTL options.

**Where:** `app/services/cache.py`

### OAuth Resilience Fixes

Three fixes to prevent OAuth token loss and unnecessary retries:

- **Transient refresh failure protection:** Exponential backoff (60s/120s/240s, max 3 retries) before treating a refresh failure as permanent. Prevents transient network errors from wiping valid tokens. (`app/services/oauth.py`)
- **429 retry guard:** Stops aggressive retry stacking on OAuth token endpoint 429 responses. (`app/services/oauth.py`)
- **Browser impersonation skip:** Disables TLS fingerprinting (`impersonate="chrome"`) for `console.anthropic.com` OAuth endpoints to avoid triggering rate limits. (`app/services/oauth.py`)

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

### Claude API Spec Alignment

Updated thinking/effort/beta headers to match the latest Claude API specification.

**Where:** `app/processors/claude_ai/claude_api_processor.py`

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
