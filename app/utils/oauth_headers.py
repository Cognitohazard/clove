"""Shared header construction for OAuth-authenticated upstream calls.

Three call sites need the same OAuth + anthropic-beta merge:
  * /v1/messages (`claude_api_processor`)
  * /v1/models (`routes/models`)
  * /v1/models discovery (`oauth.fetch_available_models`)

Centralising avoids three subtly-different copies of the same header set
(invisible drift in beta features or version pins is a real risk).
"""

from typing import Dict, Iterable, Optional

# Required for OAuth-authenticated calls to api.anthropic.com.
_OAUTH_BETA = "oauth-2025-04-20"
_DEFAULT_VERSION = "2023-06-01"


def build_oauth_headers(
    access_token: str,
    *,
    client_beta_header: Optional[str] = None,
    extra_betas: Iterable[str] = (),
    anthropic_version: Optional[str] = None,
    accept: Optional[str] = None,
    content_type: Optional[str] = None,
) -> Dict[str, str]:
    """Build the Authorization + anthropic-beta + anthropic-version header set.

    ``client_beta_header`` is the comma-separated value the inbound client sent
    (if any); duplicates are deduped against the internal beta set.
    """
    betas = [_OAUTH_BETA, *extra_betas]
    if client_beta_header:
        for beta in client_beta_header.split(","):
            beta = beta.strip()
            if beta and beta not in betas:
                betas.append(beta)

    headers: Dict[str, str] = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": ",".join(betas),
        "anthropic-version": anthropic_version or _DEFAULT_VERSION,
    }
    if accept:
        headers["Accept"] = accept
    if content_type:
        headers["Content-Type"] = content_type
    return headers
