# 请求可观测性

## 概述

Clove 内置一个轻量级的请求观测子系统：

- 每个 `/v1/*` 请求对应一个 `RequestSpan`
- 结构化的"访问日志"（`access.log`），每行一条 JSON 记录
- 所有日志行通过 loguru patcher 自动携带 `request_id`，便于跨文件关联

实现位于 `app/core/observability/`。

## 快速开启

默认**关闭**。打开结构化访问日志只需一个开关：

```bash
ACCESS_LOG_ENABLED=true
```

其他相关环境变量（均有合理默认值）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `ACCESS_LOG_ENABLED` | `false` | 是否启用 access.log |
| `ACCESS_LOG_PATH` | `logs/access.log` | 访问日志路径 |
| `ACCESS_LOG_ROTATION` | `100 MB` | loguru 轮转策略 |
| `ACCESS_LOG_RETENTION` | `14 days` | 轮转文件保留时长 |
| `ACCESS_LOG_SAMPLE_RATE_OK` | `1.0` | 成功请求采样率 (0.0–1.0) |
| `ACCESS_LOG_SAMPLE_RATE_ERROR` | `1.0` | 失败请求采样率 (0.0–1.0) |

## 日志格式

每行一条 JSON，示例（缩进为了阅读方便，实际是单行）：

```json
{
  "event": "request.complete",
  "request_id": "a3f9c2...",
  "method": "POST",
  "path": "/v1/messages",
  "status": "ok",
  "http_status": 200,
  "duration_ms": 1834,
  "model": "claude-opus-4-7",
  "stream": true,
  "upstream": "oauth",
  "account_id": "aaaaaaaa…",
  "client_key": "sk-ant…mnop",
  "input_tokens": 1240,
  "output_tokens": 318,
  "cache_read_tokens": 1100,
  "cache_write_tokens": 0,
  "error": null
}
```

字段含义：

| 字段 | 说明 |
|------|------|
| `request_id` | 请求标识，来自入参 `x-request-id` 或服务端生成 |
| `status` | `ok` / `client_error` / `rate_limited` / `auth_error` / `upstream_error` / `exception` |
| `http_status` | 实际 HTTP 状态码 |
| `duration_ms` | 请求总耗时（中间件开始到响应体发送完成） |
| `upstream` | `oauth`（OAuth API 链路）或 `web`（Claude.ai Web 链路） |
| `account_id` | 处理请求的账户，已脱敏为首 8 位 |
| `client_key` | 调用方 API Key，已脱敏为前 6 位 + 后 4 位 |
| `input_tokens` / `output_tokens` | 输入/输出 token 数 |
| `cache_read_tokens` / `cache_write_tokens` | 提示缓存命中/写入 |
| `error` | 异常类型名，仅在 `status=exception` 时非空 |

`account_id` 和 `client_key` 在写入日志前自动脱敏。原始值只保留在进程内的 span 对象里，不会落盘。

**哪些路径会记录？** 只有 `/v1/*`。`/health`、`/api/admin/*`、静态资源**不会**产生访问日志，避免冲淡真实流量。

## 响应头

开启后，`/v1/*` 所有响应（含 500）都会带 `x-request-id` 响应头。客户端若传入该请求头，服务端原样回写；否则服务端生成 `uuid4().hex`。

## stdout / app.log 关联

Patcher 会把 `request_id` 注入每条日志记录的 `extra`，stdout 格式串已改为：

```
2026-04-24 12:34:56.789 | INFO     | [a3f9c2...] Successfully processed request via Claude API
```

所以可以用 access.log 看到的 `request_id` 直接去 stdout / `app.log` 里 grep 整条请求的上下文：

```bash
grep "[a3f9c2" logs/app.log
```

非 `/v1/*` 请求或启动阶段的日志，`request_id` 显示为 `-`。

`request.complete` 事件**只进** access.log，不会污染 stdout 或 app.log（通过 loguru `filter=` 实现）。

## Docker 部署

默认的 `docker-compose.yml` 把 `./data:/data` 挂出来，直接把访问日志指到这个目录即可在宿主机读到：

```yaml
environment:
  ACCESS_LOG_ENABLED: "true"
  ACCESS_LOG_PATH: /data/access.log
```

宿主机：

```bash
tail -f data/access.log | jq -c .record.extra
```

## 常用查询（jq）

```bash
# 所有非 ok 请求
jq 'select(.record.extra.status != "ok") | .record.extra' logs/access.log

# 超过 5s 的慢请求
jq 'select(.record.extra.duration_ms > 5000) | .record.extra' logs/access.log

# 按账户统计请求数
jq -r '.record.extra.account_id' logs/access.log | sort | uniq -c | sort -rn

# 按模型累计 token
jq -s 'group_by(.record.extra.model) | map({
  model: .[0].record.extra.model,
  input:  (map(.record.extra.input_tokens)  | add),
  output: (map(.record.extra.output_tokens) | add)
})' logs/access.log

# 单个客户端的全部请求
jq 'select(.record.extra.client_key == "sk-ant…mnop") | .record.extra' logs/access.log
```

## 采样

高并发场景可以把成功请求采样率调低，错误永远保留：

```bash
ACCESS_LOG_SAMPLE_RATE_OK=0.1      # 只记 10% 的 ok
ACCESS_LOG_SAMPLE_RATE_ERROR=1.0   # 错误全部记录（默认）
```

采样在 `SampledExporter` 层实现，错误路径不会受成功率影响。

## 状态分类

中间件根据 HTTP 状态码把请求分到统一的状态桶：

| 状态 | 触发条件 |
|------|------|
| `ok` | 2xx / 3xx |
| `client_error` | 4xx（除 401/403/429） |
| `auth_error` | 401 / 403 |
| `rate_limited` | 429 |
| `upstream_error` | 5xx |
| `exception` | 中间件内部捕获的未处理异常 |

`exception` 路径下，中间件会自己构造一个 500 JSON 响应并打上 `x-request-id`，不会交给 Starlette 默认的 `ServerErrorMiddleware`——这是为了保证失败请求同样可追踪。

## 内部架构

模块划分（`app/core/observability/`）：

| 文件 | 作用 |
|------|------|
| `span.py` | `RequestSpan` 数据类、`UsageSnapshot`、`classify_status`、`mask_key` / `mask_uuid` |
| `context.py` | ContextVar 包装的 `current_span()` |
| `exporter.py` | `SpanExporter` Protocol 及 `LoguruExporter` / `MultiExporter` / `SampledExporter` / `NullExporter` |
| `middleware.py` | 纯 ASGI `RequestObservabilityMiddleware` + `build_default_exporter()` |
| `usage_tap.py` | `UsageTap` Protocol 及 `SSEUsageTap` / `JSONUsageTap` / `NullUsageTap` |

数据写入职责：

- `RequestObservabilityMiddleware`：创建 span、设置 contextvar、注入 `x-request-id`、终态写入、导出
- `ClaudeAIPipeline`：`model` / `stream`
- `ClaudeAPIProcessor` / `ClaudeWebProcessor`：`upstream` / `account_id`
- `MessageCollectorProcessor`（Web 链路）：`usage.*`
- `SSEUsageTap` / `JSONUsageTap`（OAuth 链路，按 Content-Type 调度）：`usage.*`
- `verify_api_key` / `verify_admin_api_key`：`client_key`

`SpanExporter` 是 Protocol，可以自由组合 `SampledExporter` / `MultiExporter` / 未来的 `OtelExporter`，不需要改中间件。

测试覆盖位于 `tests/`：
- `test_span.py` — 状态分类、脱敏、数据类 writer
- `test_exporter.py` — 四种 exporter 行为
- `test_middleware.py` — 状态分类、异常路径、exactly-once、x-request-id
- `test_usage_tap.py` — 跨 chunk 缓冲、content-type 分发
