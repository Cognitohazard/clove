from loguru import logger
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.core.config import settings
from app.core.error_handler import app_exception_handler
from app.core.exceptions import AppError
from app.core.static import register_static_routes
from app.utils.logger import configure_logger
from app.services.account import account_manager
from app.services.session import session_manager
from app.services.tool_call import tool_call_manager
from app.services.cache import cache_service
from app.services.proxy import proxy_service


def migrate_proxy_config():
    """
    迁移旧的 proxy_url 配置到新的 proxy 格式
    Migrate legacy proxy_url config to new proxy settings format.
    """
    import json
    import os
    from app.models.proxy import ProxySettings, ProxyMode

    # 检查是否需要迁移：proxy_url 存在且 proxy 不存在
    if not settings.proxy_url or settings.proxy is not None:
        return

    logger.info(f"Migrating legacy proxy_url to new proxy format...")

    # 创建新的 proxy 配置
    new_proxy = ProxySettings(
        mode=ProxyMode.FIXED,
        fixed_url=settings.proxy_url,
    )

    # 更新内存中的配置
    settings.proxy = new_proxy
    settings.proxy_url = None

    # 保存到 config.json
    if not settings.no_filesystem_mode:
        config_path = settings.data_folder / "config.json"
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)

                # 迁移：添加新字段，删除旧字段
                config_data["proxy"] = new_proxy.model_dump()
                config_data.pop("proxy_url", None)

                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=2, ensure_ascii=False)

                logger.info("Proxy config migration completed successfully")
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to migrate proxy config file: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting Clove...")

    configure_logger()

    # 迁移旧的代理配置
    migrate_proxy_config()

    # Load accounts
    account_manager.load_accounts()

    for cookie in settings.cookies:
        await account_manager.add_account(cookie_value=cookie)

    # Start tasks
    await account_manager.start_task()
    await session_manager.start_cleanup_task()
    await tool_call_manager.start_cleanup_task()
    await cache_service.start_cleanup_task()
    await proxy_service.initialize()

    yield

    logger.info("Shutting down Clove...")

    # Save accounts
    account_manager.save_accounts()

    # Stop tasks
    await account_manager.stop_task()
    await session_manager.cleanup_all()
    await tool_call_manager.cleanup_all()
    await cache_service.cleanup_all()
    await proxy_service.shutdown()


app = FastAPI(
    title="Clove",
    description="A Claude.ai reverse proxy",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(api_router)


# 健康检查（必须在 SPA 路由之前注册，避免被通配路由拦截）
@app.get("/health")
async def health():
    """Health check endpoint."""
    stats = await account_manager.get_status()
    readiness = await account_manager.get_api_readiness()
    return {
        "status": "healthy" if readiness["messages_ready"] else "degraded",
        "messages_ready": readiness["messages_ready"],
        "models_ready": readiness["oauth_ready"],
        "oauth_ready": readiness["oauth_ready"],
        "web_ready": readiness["web_ready"],
        "accounts": {
            "valid": readiness["valid_accounts"],
            "oauth": readiness["oauth_accounts"],
            "web": readiness["web_accounts"],
        },
        "statistics": {
            "valid_accounts": stats["valid_accounts"],
            "rate_limited_accounts": stats["rate_limited_accounts"],
            "invalid_accounts": stats["invalid_accounts"],
            "active_sessions": stats["active_sessions"],
        },
    }


# Static files (SPA fallback - must be last)
register_static_routes(app)

# Exception handlers
app.add_exception_handler(AppError, app_exception_handler)


def main():
    """Main entry point for the application."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
