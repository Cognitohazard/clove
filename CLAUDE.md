# 项目概述

Clove 是一个 Claude.ai 反向代理，本仓库是 `Cognitohazard/clove` 二开 Fork。它通过 Claude.ai 账户提供 Anthropic Claude Messages API 兼容访问，并附带一个前端管理界面。

上游关系：

- `Cognitohazard/clove` 以 `mirrorange/clove` 为直接 upstream（同步原始仓库）
- 早期通过中间 fork `Huan-zhaojun/clove`（其本身 fork 自 `mirrorange/clove`）同步，其改动已并入本仓库
- 本 Fork 主要面向 Docker/GHCR 部署，镜像为 `ghcr.io/cognitohazard/clove:latest`

支持两条 Claude 链路：

- **OAuth API 链路**：优先使用 OAuth token 直接访问 `https://api.anthropic.com/v1/messages`
- **Claude.ai Web 链路**：通过 Claude.ai 网页会话作为回退，支持图片上传、扩展思考、Web Search 映射等网页端能力

# 开发启动

## 后端开发

```bash
# 推荐先同步依赖
uv sync --extra rnet --extra dev

# 启动后端，默认端口 5201
uv run python -m app.main

# 或使用项目入口命令
uv run clove
```

访问：

- 后端 API: `http://localhost:5201`
- 健康检查: `http://localhost:5201/health`

## 前后端分离开发

```bash
# 终端 1：后端
uv run python -m app.main

# 终端 2：前端
cd front
pnpm install
pnpm dev
```

访问 `http://localhost:5173`。Vite 会把 `/api` 和 `/health` 代理到 `http://localhost:5201`。

## Docker 本地运行

```bash
docker compose up --build
```

`docker-compose.yml` 默认：

- 暴露 `5201:5201`
- 挂载 `./data:/data`
- 设置 `DATA_FOLDER=/data`
- 使用 `ghcr.io/cognitohazard/clove:latest`

# 常用命令

```bash
# 代码检查与格式化
uv run ruff check app/
uv run ruff format app/

# 前端检查与构建
cd front
pnpm lint
pnpm build

# 构建前端并打 wheel
uv run python scripts/build_wheel.py

# 仅构建 wheel，跳过前端
uv run python scripts/build_wheel.py --skip-frontend

# Makefile 等价入口
make run
make build
make build-frontend
make build-wheel
make clean
```

# 依赖管理

项目使用 **uv** 管理 Python 依赖，锁文件为 `uv.lock`，需要提交。

| 依赖组 | 用途 |
|--------|------|
| 核心 | FastAPI, Pydantic, httpx, tiktoken, uvicorn 等 |
| `rnet` | rnet HTTP 客户端，推荐链路 |
| `curl` | curl-cffi HTTP 客户端，备选链路 |
| `dev` | ruff, build 等开发工具 |

常用命令：

```bash
uv lock
uv lock --check
uv sync
uv sync --extra rnet
uv sync --all-extras
```

Dockerfile 使用 `ghcr.io/astral-sh/uv:python3.11-bookworm-slim`，并以 `uv sync --locked --no-dev --extra rnet --extra curl` 安装运行依赖。

# 后端架构

## 请求处理流程

```text
客户端请求
  -> POST /v1/messages (app/api/routes/claude.py)
     接收 raw Request；自己读 body、构造 MessagesRequestView，
     再用宽松的 MessagesAPIRequest 解析
  -> MessagesHandler (app/handlers/messages_handler.py)
     三阶段：pre-handlers / 策略 / post-chain
  -> OAuth API 响应或 Web 链路 SSE 转换响应
```

## MessagesRequestView

`app/views/messages_view.py` 是请求体的两层视图：

- 廉价访问器（`model` / `stream` / `messages` / `system` / `stop_sequences` / `raw_json` / `raw_body`）只读 raw JSON，**不会触发 Pydantic 解析**。OAuth 透明代理路径只用这些字段
- `view.parsed` 才会调用 `MessagesAPIRequest.model_validate`，专供 Web 路径访问 `thinking` / `tools` / `tool_choice` 等结构化字段
- 路由层捕获 `ValidationError` 并抛 `MalformedRequestBodyError`（400），不让 FastAPI 在边界 422 上游合法的形状

## MessagesHandler 三阶段

`app/handlers/messages_handler.py` 替代旧的 `ClaudeAIPipeline`，把控制流写成显式阶段而不是 `stop_pipeline` / `skip_processors` 标志：

1. **Pre-handlers**
   - `TestMessageProcessor` 处理 SillyTavern 测试消息（可能直接产生 canned 响应）
   - `ToolResultProcessor` 处理工具调用恢复（可能播种 `original_stream`）
2. **策略选择**（仅当上一阶段没有产出 response 也没有 original_stream 时执行）
   - `ClaudeAPIProcessor` —— OAuth 透明代理路径，成功即直接产出 `StreamingResponse`
   - `ClaudeWebProcessor` —— Claude.ai Web 回退路径，构建 SSE 流
3. **Post-chain**（仅当 `original_stream` 存在时执行）
   - `EventParsingProcessor` 解析 Claude.ai SSE 事件
   - `ModelInjectorProcessor` 注入模型信息
   - `StopSequencesProcessor` 处理停止序列
   - `ToolCallEventProcessor` 处理工具调用事件
   - `MessageCollectorProcessor` 收集消息内容
   - `TokenCounterProcessor` 估算 token 用量
   - `StreamingResponseProcessor` 格式化流式响应
   - `NonStreamingResponseProcessor` 格式化非流式响应

`ClaudeAIContext` 在阶段间传递 view、解析后的请求、会话、原始流、响应和元数据。处理器内部不再写 `stop_pipeline`，也不需要防御性的 `if context.response: return context`。

## OAuth API 链路要点

核心文件：`app/processors/claude_ai/claude_api_processor.py`

- OAuth 链路优先执行，成功后设置 `context.metadata["stop_pipeline"] = True`
- 直接使用原始 request body 转发，避免 Pydantic round-trip 丢失未知字段
- `BaseModel` 默认 `extra="allow"`，新 Anthropic 字段应尽量透明透传
- `MessagesAPIRequest` 内的字段已尽量去掉 `Literal` / 数值上界（`effort`、`thinking.type`、`tool_choice.type`、`cache_control.ttl`、`temperature`、`top_p`、`media_type` 等），避免 FastAPI 在路由层对上游合法值返回 422。新增上游枚举值时无需再改这里
- 默认会注入 legacy Claude Code system prompt，可通过 `INJECT_CLAUDE_CODE_SYSTEM_PROMPT=false` 关闭
- `anthropic-beta` 会合并内部 `oauth-2025-04-20` 与客户端传入值
- `invalid_request_error` 被视为不可重试错误
- 403 空响应会把当前代理标记为 unhealthy

## Claude.ai Web 链路要点

核心文件：`app/processors/claude_ai/claude_web_processor.py`

- 用于 OAuth API 不可用时的回退
- 图片上传走会话级 wiggle/upload 端点，上传失败会中止请求
- 单次 Web 请求最多 20 个文件，超限会提前报错
- 支持纯图片请求
- `web_search_*` server tool 会映射为 Claude.ai Web 端 `web_search_v0`
- `thinking.enabled` 或 `thinking.adaptive` 会启用网页端 extended/paprika 模式

# 请求可观测性

代码位于 `app/core/observability/`，注册为最外层 ASGI 中间件。

组件：

| 文件 | 用途 |
|------|------|
| `span.py` | `RequestSpan` 数据类，`UsageSnapshot`，`classify_status`，`mask_key`/`mask_uuid` |
| `context.py` | ContextVar 包装的 `current_span()` |
| `exporter.py` | `SpanExporter` Protocol 及 `LoguruExporter` / `MultiExporter` / `SampledExporter` / `NullExporter` |
| `middleware.py` | 纯 ASGI `RequestObservabilityMiddleware`，包含 `build_default_exporter()` |
| `usage_tap.py` | `UsageTap` Protocol 及 `SSEUsageTap` / `JSONUsageTap` / `NullUsageTap` |

关键行为：

- 只对 `/v1/*` 请求启 span，`/api/admin`、`/health`、静态资源不记录
- `x-request-id` 来自请求头；缺省则生成 `uuid4().hex`，响应头回写
- 未处理异常由中间件自身捕获并返回 JSON 500，保证 `x-request-id` 能写入响应头；不会冒泡到 Starlette 默认的 `ServerErrorMiddleware`
- Pipeline 各处理器通过 `current_span()` 写入属性：
  - `ClaudeAIPipeline.process` 写 `model` / `stream`
  - `ClaudeAPIProcessor` 写 `upstream="oauth"` / `account_id`
  - `ClaudeWebProcessor` 写 `upstream="web"` / `account_id`
  - `MessageCollectorProcessor` 写 `usage.*`（Web 链路）
  - OAuth 链路额外使用 `create_usage_tap(span, content_type)`；SSE 走 `EventParser`，JSON 走整体缓冲解析
  - `verify_api_key` / `verify_admin_api_key` 写 `client_key`
- 终态信息由中间件写入：`status`（`ok`/`client_error`/`rate_limited`/`auth_error`/`upstream_error`/`exception`）、`http_status`、`error`
- `to_record()` 输出时自动 mask `account_id` 和 `client_key`
- `app/utils/logger.py` 通过 `logger.configure(patcher=...)` 把 `request_id` / `model` / `account_id` / `client_key` 注入每条日志的 `extra`，使任意位置的 log 都能 grep 按请求
- `request.complete` 记录用 `filter=` 分流到 `access.log`（`serialize=True`，按配置轮转）；stdout/app.log 过滤掉该事件

测试位于 `tests/`，覆盖 span 数据类、四种 exporter、四种状态场景的中间件行为、`UsageTap` 分发与跨 chunk 解析。

# 核心服务

| 服务 | 文件 | 用途 |
|------|------|------|
| `account_manager` | `app/services/account.py` | 账户生命周期、负载均衡、状态恢复、OAuth token 刷新、按 model 选择账户 |
| `session_manager` | `app/services/session.py` | Claude.ai Web 会话管理 |
| `tool_call_manager` | `app/services/tool_call.py` | 待处理工具调用追踪 |
| `cache_service` | `app/services/cache.py` | 响应缓存与 checkpoint/account 绑定 |
| `oauth_authenticator` | `app/services/oauth.py` | OAuth 认证、token exchange、refresh |
| `proxy_service` | `app/services/proxy.py` | 固定代理和动态代理池，轮换、冷却、健康状态 |
| `i18n_service` | `app/services/i18n.py` | 国际化翻译管理 |
| `event_processing` | `app/services/event_processing/` | SSE 事件解析与序列化 |

账户模型支持 `cookie_only`、`oauth_only`、`both` 三种认证类型。OAuth refresh 对临时失败有退避保护，达到最大重试次数后才会降级或标记账号失效。

`cookie_only` 账户会自愈：`Account.needs_oauth_upgrade` 为真时，手动/批量刷新与周期循环都会重新尝试 cookie→OAuth 升级（不再只在 `add_account` 时跑一次），覆盖端点曾宕机或被降级的账户。所有触发点共用全局信号量 `MAX_CONCURRENT_OAUTH_UPGRADES=3` + 抖动冷却，避免对上游打风暴。

`Account.available_models` 缓存上游 `/v1/models` 返回的模型列表。`add_account` 时后台 best-effort 拉取一次，之后 `refresh_account_status` 周期性更新；任何失败都保留现有缓存而非清零。`get_account_for_oauth(model=...)` 优先匹配 `can_serve_model(model) is True` 的账户，未发现的账户作为 fallback；`MAX_MODELS` 静态列表仍是 capabilities 不明时的兜底信号。

# API 路由

路由挂载在 `app/api/main.py`：

| 路径 | 文件 | 说明 |
|------|------|------|
| `POST /v1/messages` | `app/api/routes/claude.py` | Claude Messages API 兼容入口 |
| `GET /v1/models` | `app/api/routes/models.py` | 通过 OAuth 账户透明代理 Anthropic models 列表 |
| `GET /v1/models/{model_id}` | `app/api/routes/models.py` | 通过 OAuth 账户透明代理模型详情 |
| `/api/admin/accounts` | `app/api/routes/accounts.py` | 账户增删改查 |
| `/api/admin/accounts/oauth/exchange` | `app/api/routes/accounts.py` | OAuth 授权码交换 |
| `/api/admin/accounts/batch/refresh` | `app/api/routes/accounts.py` | 批量刷新账户状态 |
| `/api/admin/accounts/batch/delete` | `app/api/routes/accounts.py` | 批量删除账户 |
| `/api/admin/settings` | `app/api/routes/settings.py` | 运行时配置读取与保存 |
| `/api/admin/proxies` | `app/api/routes/proxies.py` | 动态代理列表读写 |
| `/api/admin/proxies/status` | `app/api/routes/proxies.py` | 代理池状态 |
| `/api/admin/statistics` | `app/api/routes/statistics.py` | 账户统计 |
| `GET /health` | `app/main.py` | 健康检查和 readiness |

认证规则：

- `/v1/*` 使用 `API_KEYS` 或 `ADMIN_API_KEYS`
- `accounts`、`settings`、`statistics` 路由使用 `ADMIN_API_KEYS`
- `proxies` 路由当前没有声明 `AdminAuthDep`，改动认证行为前先核对前后端影响
- 未配置 `ADMIN_API_KEYS` 时会生成临时 admin key，只打印到启动日志，不会持久化

# 配置与数据

配置优先级从高到低：

1. 初始化参数
2. JSON 配置文件：`DATA_FOLDER/config.json`
3. 环境变量
4. `.env`
5. 默认值

默认数据目录为 `~/.clove/data/`，Docker 中通常为 `/data`。

数据文件：

- `accounts.json` - 账户、cookie、OAuth token、状态
- `config.json` - 管理端保存的运行时配置
- `proxies.txt` - 动态代理池列表，每行一个代理

重要配置：

- `HOST`, `PORT`, `DATA_FOLDER`
- `API_KEYS`, `ADMIN_API_KEYS`
- `COOKIES`
- `CLAUDE_AI_URL`, `CLAUDE_API_BASEURL`
- `OAUTH_TOKEN_URL` token 端点，默认 `https://claude.ai/v1/oauth/token`（`console.anthropic.com/v1/oauth/token` 已被 Anthropic 下线返回 404，`api.anthropic.com/v1/oauth/token` 为已知可用 fallback）
- `OAUTH_AUTHORIZE_URL` / `OAUTH_REDIRECT_URI` OAuth 授权与回调地址；URL 类设置有 `before` 校验器会剥离首尾多余引号/空白（规避 docker-compose `- OAUTH_TOKEN_URL="..."` 把引号当字面量的坑）
- `MAX_CONCURRENT_OAUTH_UPGRADES` 默认 `3`，限制全局并发 cookie→OAuth 升级
- `INJECT_CLAUDE_CODE_SYSTEM_PROMPT`
- `PROXY_URL` 旧固定代理配置，启动时会迁移到新 `proxy` 配置
- `NO_FILESYSTEM_MODE` 会禁用文件读写，账户和配置只保存在内存中
- `MAX_MODELS` 默认包含 `claude-opus-4-6`、`claude-opus-4-7` 与 `claude-opus-4-8`，用于选择 Max 账户
- `ACCESS_LOG_ENABLED`（默认 `false`）开启结构化请求访问日志
- `ACCESS_LOG_PATH`（默认 `logs/access.log`）访问日志路径
- `ACCESS_LOG_ROTATION`（默认 `100 MB`）/ `ACCESS_LOG_RETENTION`（默认 `14 days`）轮转与保留
- `ACCESS_LOG_SAMPLE_RATE_OK`（默认 `1.0`）成功请求采样率；`ACCESS_LOG_SAMPLE_RATE_ERROR`（默认 `1.0`）失败请求采样率

# 动态代理

代理配置模型在 `app/models/proxy.py`，服务实现在 `app/services/proxy.py`。

模式：

- `disabled` - 不使用代理
- `fixed` - 单固定代理
- `dynamic` - 从 `proxies.txt` 加载代理池

轮换策略：

- `sequential`
- `random`
- `random_no_repeat`
- `per_account`

代理发生连接错误、403 空响应等情况时会进入 cooldown，`proxy_service` 会在可用代理间切换。

# 前端子模块

前端位于 `front/`，是独立子模块，当前指向 `Cognitohazard/clove-front`。技术栈：

- React 19
- TypeScript
- Vite 7
- Tailwind CSS 4
- Radix UI
- Axios

主要入口：

- `front/src/api/client.ts` - API 封装和 admin key 注入
- `front/src/api/types.ts` - 前端接口类型
- `front/src/pages/Accounts.tsx` - 账户管理
- `front/src/pages/Dashboard.tsx` - 仪表盘
- `front/src/pages/Settings.tsx` - 设置页
- `front/src/components/DynamicProxySettings.tsx` - 动态代理配置

更多前端说明见 `front/CLAUDE.md`。

构建部署：

```bash
cd front
pnpm install
pnpm build
cp -r dist/* ../app/static/
```

`scripts/build_wheel.py` 会自动构建前端并复制到 `app/static/`，除非传入 `--skip-frontend`。

# 关键开发模式

- 全异步 I/O，服务和处理器使用 `async/await`
- Pydantic v2 模型在 `app/models/`
- 项目 Claude API 模型默认 `extra="allow"`，不要轻易改回 forbid
- HTTP 客户端统一走 `app/core/http_client.py`
- OAuth token exchange 使用 plain session，避免浏览器 TLS 指纹导致 429
- 日志使用 Loguru：`from loguru import logger`
- 异常类型集中在 `app/core/exceptions.py`，FastAPI handler 在 `app/core/error_handler.py`
- 静态资源和 SPA fallback 在 `app/core/static.py`，必须晚于 API 和 `/health` 注册

# 合并上游注意事项

本仓库既保留本地定制，也会合并上游改动。上游 PR 被合并后，同一逻辑可能在本地和上游以不同 SHA 出现，自动合并可能保留两份实现。

建议流程：

```bash
git fetch upstream
git merge --no-commit upstream/main
git diff --cached
```

合并后必须检查：

1. 是否出现重复 class/function 定义
2. 本 Fork 定制是否被上游覆盖
3. OAuth 原始 body 透传、models proxy、动态代理、Docker/GHCR 配置是否仍正确
4. 前端子模块是否仍指向 `Cognitohazard/clove-front`

CI 中有每日 `auto-merge-upstream.yml`，冲突时会创建 issue。镜像发布由 `docker-publish.yml` 负责。

# 文档索引

| 文档 | 说明 |
|------|------|
| `README.md` | Fork 差异、部署入口、上游新增能力 |
| `docs/proxy-settings.md` | 代理模式、轮换策略、健康管理 |
| `docs/account-management-enhance.md` | 多账户搜索/筛选/排序/分页/批量操作 |
| `docs/web-search-analysis.md` | Web Search 机制分析和双链路方案 |
| `docs/anthropic-standard-streaming-notes.md` | 流式输出标准化策略、事件映射矩阵、引用标准化 |
| `docs/overloaded-error-analysis.md` | 503 Overloaded 根因分析与优化方案 |
| `docs/hatch-build-issue.md` | Hatch 构建 force-include 问题 |
| `docs/rnet-version-issue.md` | rnet 代理用法与 3.x 升级记录 |
