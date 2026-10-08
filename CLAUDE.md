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
- body 以 `view.raw_json`（普通 dict，不经 Pydantic round-trip）为基础，只加上 Claude Code 身份字段后重新序列化，未知字段原样保留；详见下文「Claude Code 身份伪装」
- `BaseModel` 默认 `extra="allow"`，新 Anthropic 字段应尽量透明透传
- `MessagesAPIRequest` 内的字段已尽量去掉 `Literal` / 数值上界（`effort`、`thinking.type`、`tool_choice.type`、`cache_control.ttl`、`temperature`、`top_p`、`media_type` 等），避免 FastAPI 在路由层对上游合法值返回 422。新增上游枚举值时无需再改这里
- `anthropic-beta` 为 `claude-code-20250219,oauth-2025-04-20` 加客户端传入值（去重、保序）
- `invalid_request_error` 被视为不可重试错误
- 403 空响应会把当前代理标记为 unhealthy；传输层错误（连接被拒、重试后仍被关闭/重置、TLS 失败）同样标记并返回 `ProxyConnectionError`。超时不标记：`REQUEST_TIMEOUT` 是整个请求的总时长，慢生成不代表代理坏了

## Claude Code 身份伪装 (identity layer)

Clove 发往 `CLAUDE_API_BASEURL` 的每一次 OAuth 调用（`/v1/messages`、`/v1/models` 代理、`/v1/models` 模型发现、限流探测）在 TLS、HTTP、Header、计费字段四层都与真实 Claude Code CLI 一致。Claude.ai Web 链路和 OAuth token 端点（仍走 plain httpx）不在范围内。线上格式的逐字段说明（billing 字段顺序、header 顺序、TLS profile、刻意不复制的部分）见 `docs/claude-code-wire-identity.md`。

- `app/utils/claude_code_identity.py`：纯函数（`finalize_messages_body` / `build_messages_headers` / `build_api_headers` / `compute_cch` / `verify_billing` 等）加进程内版本状态。其中的常量（`HEADER_ORDER` / `STATIC_HEADERS` / `PLATFORM_HEADERS` / `TLS_PROFILE` / `FALLBACK_CLI_VERSION`）是**运行时唯一真相来源**；测试断言它们与 `tests/fixtures/claude_code_fingerprint.json` 一致，运行时从不读 fixture（Docker 镜像里没有 `tests/`）
- 传输层：`app/core/http_client.py` 的 `create_oauth_session(proxy)`，rnet 不开浏览器仿真，显式配置 TLS（无 GREASE、ALPN 仅 `http/1.1`、X25519MLKEM768 等）、HTTP/1.1、`orig_headers` 控制 header 顺序与大小写；每个代理 URL 缓存一个 client 以复用连接和 TLS 会话；只用系统信任根。连接被关闭/重置时在新连接上重试一次，重试那次带 `X-Stainless-Retry-Count: 1`（rnet 分不清失效的复用连接和请求发出后才断开的新连接，所以非幂等的 POST 可能被上游收到两次，CLI 用的 Stainless SDK 也是这样重试的）。依赖 `rnet` extra：没有它时 `create_oauth_session` 抛 `RuntimeError`，不回退到其他 HTTP 客户端，分到 OAuth 账户的请求直接报错；Web 链路不依赖 rnet，没有可用 OAuth 账户时照常回退过去。Docker 镜像始终装有 rnet
- 所有 OAuth 客户端走同一条路径，不保留"原生 Claude Code 直通"：版本号、UA、Stainless header、`device_id`、`account_uuid`、TLS 一律用 Clove 自己的，避免旧客户端的版本号撞上按模型的版本门禁。但客户端提供的**合法值会被采用**：JSON `metadata.user_id` 里的 v4 `session_id`、v4 `x-claude-code-prompt-id`；没有时再派生。客户端的 `x-claude-code-request-class` **不采用**：代理的请求一律以 `main` 发出（Clove 唯一发出的非 main 请求是自己的限流探测），否则会出现「auxiliary 类 + main 的完整 billing 块与 prompt id」这种 CLI 从不发送的半成品形状
- body：在 `view.raw_json` 上插入 billing 块、identity 块和 `metadata.user_id`，紧凑序列化后计算 `cch` 回填。未知字段与客户端键顺序保留，只有新插入的键放到 CLI 的位置；`NaN` / `Infinity` 返回 400
- `system` = `[billing, identity, ...客户端 system]`：客户端自带的 billing 块被丢弃；客户端 system 的首个块文本恰为 identity 行时，**保留客户端那块（含它的 `cache_control`）**，不再插入我们的；`""` / `[]` 视为缺省
- billing 块字段顺序为 `cc_version` / `cc_entrypoint` / `cch` / `cc_prev_req` / `cc_prompt_id` / `cc_turn_origin` / `cc_prompt_index` / `cc_turn_index`。`cc_prev_req` 是同一会话上一个 **main** 响应的 `request-id`（代理的请求都是 main，限流探测不计），取自进程内有界 map（键是实际发出的 session id，采用了客户端的 id 时就是那个 id；最多 4096 条，不持久化），重启后每个会话的第一个请求没有该字段
- `cc_turn_index` 是 CLI 的轮次序号，`cc_prompt_index` 是其中由人类发起的轮次序号，工具循环的 `tool_result` 续请求沿用所在轮次的值（2.1.293 抓包与 CLI 源码确认）。Clove 发出的轮次都是 `cc_turn_origin=human`，所以两者都取人类轮数（与 prompt id 用同一个计数：不含 `tool_result` 块的 user 消息条数）；人类轮数为 0 时两个字段都省略（CLI 无法定位轮次时也省略）
- `metadata.user_id` 是 **JSON 字符串**，键顺序 `device_id` / `account_uuid` / `session_id`。`device_id` 每账户生成一次并随账户持久化（`NO_FILESYSTEM_MODE` 下每次重启都会变；回滚到旧镜像会丢失后重新生成）；`account_uuid` 存在 `OAuthToken` 上，来自 token 响应的 `account.uuid`，粘贴的 token 为 `""`，refresh 响应不带时沿用原值
- session id / prompt id 无状态派生且保持 UUIDv4 形态：session id 基于 `device_id` + 去掉 `cache_control` 的 `messages[0]`，prompt id 基于 session id + 人类轮数（不含 `tool_result` 块的 user 消息才算新一轮）。裁剪或压缩旧消息的客户端会让 id 变化
- `INJECT_CLAUDE_CODE_SYSTEM_PROMPT=false` **只省略 identity 块**；billing 块、`metadata.user_id`、header 和 TLS 始终生效，没有"整层关闭"的开关
- 版本：固定为抓包时的 CLI 版本 `FALLBACK_CLI_VERSION`（当前 `2.1.293`，与 fixture 的 `_cli_version` 一致），运行时**不跟随 npm**：新版 CLI 可能改了线上格式，只换版本号会发出「新版本号 + 旧版本布局」这种真实 CLI 从不发送的组合。上游返回 400 `... version Y or newer is required` 时升到 Y（只升不降，仅在本进程内有效）并重试一次，这是安全网而不是升级途径；升级途径是下文的每日漂移检测 + 重新抓包。`CLAUDE_CODE_VERSION` 覆盖默认版本并关闭门禁自动升级（门禁 400 直接返回给客户端），**只能**通过环境变量设置，不在设置 API / 管理页里：管理页每次保存设置都会重写 `config.json` 并丢掉未知键，写在 `config.json` 里的值下次保存后就没了
- `/v1/models`（路由代理与模型发现）用同一套 software header（去掉 session / prompt / request-class / `Content-Type`），`anthropic-version` 固定；**不再透传客户端的 `anthropic-version` / `anthropic-request-id`**
- **合并前必须跑的真实 token 冒烟**（用户手动）：`CLOVE_OAT=<token> uv run python scripts/probe_oauth.py --messages`，覆盖版本门禁模型的流式 + 非流式 200、重复前缀的 `cache_read_input_tokens > 0`、`/v1/models`。429 解析无法强制触发，不在覆盖范围内

### CLI 漂移检测

Claude Code 每次发版都可能改变上述任一项（尤其是 Stainless SDK 版本号、beta 列表、TLS 参数）。`scripts/capture_claude_code.py` 从 npm 下载 linux-x64 包（先按 registry 的 `dist.integrity` 校验 sha512），在本地 mitmproxy 后面真实跑一次 Claude Code CLI（所有请求在本地应答，不会打到 Anthropic），提取结构指纹并与 fixture 比对：

```bash
# 切片收尾的门禁：只抓 fixture 自己的 CLI 版本，与 npm latest 无关
uv run python scripts/capture_claude_code.py --check --version fixture

# 抓 npm latest 与 fixture 比对（每日 CI 用）
uv run python scripts/capture_claude_code.py --check

# 走 Clove 自己的 OAuth 传输层发请求，与同一份 fixture 比对
uv run python scripts/capture_claude_code.py --clove

# 重新抓取并写入 fixture
uv run python scripts/capture_claude_code.py --write tests/fixtures/claude_code_fingerprint.json
```

- 退出码：`0` 一致；`1` drift（必需字段不同、billing 块验证失败，或抓到的请求已不是 harness 能解析的形状，如没有 billing 块、UA 格式变了）；`2` 不可用（`--clove` 导入不到身份层或 `create_oauth_session`，或没有 rnet）；`3` harness 自身出错（下载、校验、运行抓包、读抓包日志、超时，以及任何意外异常）。以 `_` 开头的字段（版本号、beta 列表、body 键）只打印，不算 drift
- `--clove` 不覆盖任何证书信任：同一个 mitm 代理上先发一次 https 请求（mitm 记下 ClientHello，随后客户端拒绝 mitm 证书、请求失败——这正是预期），再以相同 header 与 body 发一次 plain http 请求（addon 本地应答）。TLS 取自第一次，其余取自第二次。第一次请求如果成功，说明传输层不校验证书，按 drift（`1`）退出
- 没有单独的总超时：各步骤自带超时，CI 用 job 的 `timeout-minutes` 兜底
- 出现 drift（`claude-code-drift` issue）就是升级 CLI 版本的信号：用 `--write` 重新抓包写入 fixture，按新指纹更新 `app/utils/claude_code_identity.py` 的常量（含 `FALLBACK_CLI_VERSION`；billing 字段这类格式变化还要改代码），再跑测试，两者一致由测试保证。新字段的语义要靠多轮 + 工具循环的抓包确认，单次 `--write` 只抓第一个 main 请求
- `.github/workflows/claude-code-drift.yml` 每天跑一次，分两个 job：`capture` 只有 `contents: read`，跑 `--check`（latest）和 `--clove`，把输出和各自的退出码作为 artifact 上传；`report` 只有 `issues: write`，不 checkout、不执行任何抓到的东西，只读 artifact，退出码 `1` 记到 `claude-code-drift` 标签的 issue，`2` / `3`（或 capture job 没产出结果）记到 `claude-code-harness` 标签的 issue。每条正文都带 `<!-- drift-sha256: … -->`，输出与上一次相同则不重复评论
- 指纹文件只保留结构信息（TLS 参数、header 顺序、字段名/位置），不包含 prompt 内容、路径或 token

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

自愈有一个**永久终止条件**：cookie→OAuth authorize 返回 403 且 `error.details.error_code == "session_stale_relogin"`（"Session is not fresh enough to grant elevated access"）时，说明该 cookie 背后的登录事件太旧，Anthropic 要求重新交互式登录才肯授予 OAuth。此时 `authenticate_account` 会写入 `Account.oauth_upgrade_blocked_reason`（随账户持久化），`needs_oauth_upgrade` 转为 False，自愈循环不再重试——否则会以固定节奏永远打一个必然失败的上游请求。**cookie 被替换时（`add_account` 更新已有账户 / `PUT /api/admin/accounts/{uuid}`）该标记自动清除**，因为新 cookie 可能来自一次新登录。注意 cookie 仍对 Web 链路有效：`/api/organizations` 照常 200，只有 authorize 这一步被拒。

OAuth 排障请先跑 `uv run python scripts/probe_oauth.py`（`CLOVE_SK=<sessionKey>` 可选，用于跑完整 cookie 链路）。它用 app 自己的 HTTP 客户端逐步打印上游原始响应体，避免 TLS 指纹差异带来的假象。加 `--messages` 并设置 `CLOVE_OAT=<token>` 则跑真实 `/v1/messages` 冒烟（见「Claude Code 身份伪装」）。

`Account.available_models` 缓存上游 `/v1/models` 返回的模型列表，并随账户持久化。账户**获得 OAuth token 时（add / cookie→OAuth 自愈升级 / refresh）同步发现一次**，之后 `refresh_account_status` 周期性更新（6h TTL）；任何失败都保留现有缓存而非清零。`get_account_for_oauth(model=...)` 只在 `can_serve_model(model) is True` 的账户里选择——尚未发现（或发现失败）的账户**不承接带模型的请求**，直到其 `/v1/models` 成功为止；没有可选账户则抛 `NoAccountsAvailableError`。已无静态 `MAX_MODELS` 列表需要维护：发现即唯一真相来源。

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
- `OAUTH_TOKEN_URL` token 端点，默认 `https://claude.ai/v1/oauth/token`（`console.anthropic.com/v1/oauth/token` 已被 Anthropic 下线返回 404；`api.anthropic.com/v1/oauth/token` 与 `platform.claude.com/v1/oauth/token` 均为已实测可用的 fallback，三者对同一 payload 行为一致）
- token 端点的 `authorization_code` 授权**必须带 `state`**，否则返回不指名字段的 `invalid_request_error / "Invalid request format"`；`exchange_token` 依次从 `code` 的 `#state` 尾巴、调用方显式传入的 `state`、`verifier` 兜底取值。`refresh_token` 授权不受此限制，form 与 JSON 两种编码都接受
- 管理端手动 OAuth 流程的 PKCE（verifier/challenge/state）由后端 `POST /api/admin/accounts/oauth/pkce` 生成（`oauth_authenticator.generate_pkce()`），**不在浏览器里算**：浏览器唯一的 SHA-256 是 `crypto.subtle`，它只存在于安全上下文（https 或 localhost），通过 `http://<服务器IP>` 打开管理端时为 `undefined`，会让「开始授权」直接报「生成授权 URL 失败」。`navigator.clipboard` 同属安全上下文限制（复制按钮在 http 下会静默失败，已 try/catch，仅降级）
- `OAUTH_AUTHORIZE_URL` / `OAUTH_REDIRECT_URI` OAuth 授权与回调地址；URL 类设置有 `before` 校验器会剥离首尾多余引号/空白（规避 docker-compose `- OAUTH_TOKEN_URL="..."` 把引号当字面量的坑）
- `MAX_CONCURRENT_OAUTH_UPGRADES` 默认 `3`，限制全局并发 cookie→OAuth 升级
- `INJECT_CLAUDE_CODE_SYSTEM_PROMPT`（默认 `true`）：`false` 只省略 OAuth 请求里的 Claude Code identity 块，billing 块、`metadata.user_id`、header、TLS 不受影响
- `CLAUDE_CODE_VERSION` 覆盖 OAuth 链路呈现的 Claude Code 版本（默认是抓包版本 `FALLBACK_CLI_VERSION`），并关闭版本门禁自动升级；仅环境变量（管理页保存设置会重写 `config.json` 并丢掉该键），不在设置 API 里
- `PROXY_URL` 旧固定代理配置，启动时会迁移到新 `proxy` 配置
- `NO_FILESYSTEM_MODE` 会禁用文件读写，账户和配置只保存在内存中
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
| `docs/claude-code-wire-identity.md` | OAuth 链路模仿的 Claude Code 线上格式：header 顺序、TLS profile、billing / cch / id 规则、刻意不复制的部分 |
