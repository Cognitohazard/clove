import os
import json
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl

from app.dependencies.auth import AdminAuthDep
from app.core.config import Settings, settings
from app.models.proxy import ProxySettings
from app.services.proxy import proxy_service


class SettingsRead(BaseModel):
    """Model for returning settings."""

    api_keys: List[str]
    admin_api_keys: List[str]

    proxy_url: str | None  # 已废弃，仅用于向后兼容显示旧配置
    proxy: Optional[ProxySettings] = None

    claude_ai_url: HttpUrl
    claude_api_baseurl: HttpUrl

    custom_prompt: str | None
    inject_claude_code_system_prompt: bool
    use_real_roles: bool
    human_name: str
    assistant_name: str
    padtxt_length: int
    allow_external_images: bool

    preserve_chats: bool

    oauth_client_id: str
    oauth_authorize_url: str
    oauth_token_url: str
    oauth_redirect_uri: str


class SettingsUpdate(BaseModel):
    """Model for updating settings."""

    api_keys: List[str] | None = None
    admin_api_keys: List[str] | None = None

    # proxy_url 已废弃，不再接受更新
    proxy: Optional[ProxySettings] = None

    claude_ai_url: HttpUrl | None = None
    claude_api_baseurl: HttpUrl | None = None

    custom_prompt: str | None = None
    inject_claude_code_system_prompt: bool | None = None
    use_real_roles: bool | None = None
    human_name: str | None = None
    assistant_name: str | None = None
    padtxt_length: int | None = None
    allow_external_images: bool | None = None

    preserve_chats: bool | None = None

    oauth_client_id: str | None = None
    oauth_authorize_url: str | None = None
    oauth_token_url: str | None = None
    oauth_redirect_uri: str | None = None


router = APIRouter()


@router.get("", response_model=SettingsRead)
async def get_settings(_: AdminAuthDep) -> Settings:
    """Get current settings."""
    return settings


@router.put("", response_model=SettingsUpdate)
async def update_settings(_: AdminAuthDep, updates: SettingsUpdate) -> Settings:
    """Update settings and save to config.json."""
    update_dict = updates.model_dump(exclude_unset=True)

    # 将 proxy 字典转换为 ProxySettings 对象（避免 Pydantic 序列化警告）
    if "proxy" in update_dict and isinstance(update_dict["proxy"], dict):
        update_dict["proxy"] = ProxySettings.model_validate(update_dict["proxy"])

    if not settings.no_filesystem_mode:
        config_path = settings.data_folder / "config.json"
        settings.data_folder.mkdir(parents=True, exist_ok=True)

        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = SettingsUpdate.model_validate_json(f.read())
            except (json.JSONDecodeError, IOError):
                config_data = SettingsUpdate()
        else:
            config_data = SettingsUpdate()

        config_data = config_data.model_copy(update=update_dict)

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                # Format JSON with indent for readability
                f.write(config_data.model_dump_json(exclude_unset=True, indent=2))
        except IOError as e:
            raise HTTPException(
                status_code=500, detail=f"Failed to save config: {str(e)}"
            )

    for key, value in update_dict.items():
        if hasattr(settings, key):
            setattr(settings, key, value)

    # 如果代理配置变更，通知 proxy_service 重新加载
    if "proxy" in update_dict:
        await proxy_service.reload_settings()

    return settings
