import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.core.config import settings
from app.core.http_client import RequestException, create_session
from app.dependencies.auth import AuthDep
from app.core.exceptions import NoAccountsAvailableError, ProxyConnectionError
from app.services.account import account_manager
from app.services.proxy import proxy_service


router = APIRouter()


def _prepare_headers(access_token: str, original_request: Request) -> dict[str, str]:
    beta_features = ["oauth-2025-04-20"]
    client_beta = original_request.headers.get("anthropic-beta", "")
    if client_beta:
        for beta in client_beta.split(","):
            beta = beta.strip()
            if beta and beta not in beta_features:
                beta_features.append(beta)

    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-beta": ",".join(beta_features),
        "anthropic-version": original_request.headers.get(
            "anthropic-version", "2023-06-01"
        ),
        "Accept": "application/json",
    }

    request_id = original_request.headers.get("anthropic-request-id")
    if request_id:
        headers["anthropic-request-id"] = request_id

    return headers


async def _proxy_models_request(
    original_request: Request,
    path: str,
    query_params: Optional[dict[str, str]] = None,
) -> Response:
    account = await account_manager.get_account_for_oauth()
    if not account or not account.oauth_token:
        raise NoAccountsAvailableError()

    upstream_url = settings.claude_api_baseurl.encoded_string().rstrip("/") + path
    proxy_url = await proxy_service.get_proxy(account_id=account.organization_uuid)
    session = create_session(
        proxy=proxy_url,
        timeout=settings.request_timeout,
        impersonate="chrome",
        follow_redirects=False,
    )

    try:
        response = await session.request(
            "GET",
            upstream_url,
            headers=_prepare_headers(account.oauth_token.access_token, original_request),
            params=query_params,
        )
    except RequestException as exc:
        if proxy_url:
            await proxy_service.mark_unhealthy(
                proxy_url, reason=f"connection error: {type(exc).__name__}"
            )
        raise ProxyConnectionError(proxy_url=proxy_url, error_type=type(exc).__name__)
    finally:
        await session.close()

    filtered_headers = {}
    for key, value in response.headers.items():
        if key.lower() in {"content-length", "content-encoding"}:
            continue
        if key.lower().startswith("anthropic-"):
            filtered_headers[key] = value

    content_type = response.headers.get("content-type", "")
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Model not found")

    if "json" in content_type.lower():
        try:
            data = json.loads(body.decode("utf-8"))
            return JSONResponse(
                content=data,
                status_code=response.status_code,
                headers=filtered_headers,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

    media_type = content_type or None
    return Response(
        content=body,
        status_code=response.status_code,
        headers=filtered_headers,
        media_type=media_type,
    )


@router.get("/models", response_model=None)
async def list_models(
    request: Request,
    _: AuthDep,
    before_id: Optional[str] = Query(default=None),
    after_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    query_params = {
        key: value
        for key, value in {
            "before_id": before_id,
            "after_id": after_id,
            "limit": str(limit),
        }.items()
        if value is not None
    }
    return await _proxy_models_request(
        original_request=request,
        path="/v1/models",
        query_params=query_params,
    )


@router.get("/models/{model_id}", response_model=None)
async def get_model(model_id: str, request: Request, _: AuthDep):
    return await _proxy_models_request(
        original_request=request,
        path=f"/v1/models/{model_id}",
    )
