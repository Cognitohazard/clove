# Claude Code 线上身份（wire identity）

本文记录 Clove 在 OAuth 链路上模仿的 Claude Code CLI 线上格式：真实 CLI 发什么、Clove 发什么、哪些
刻意不复制。运行时的唯一真相来源是 `app/utils/claude_code_identity.py` 里的常量；
`tests/fixtures/claude_code_fingerprint.json` 是抓包得到的结构指纹，由测试钉住两者一致，运行时从不读取。
重新抓包与漂移检测见 `scripts/capture_claude_code.py` 与 CLAUDE.md「CLI 漂移检测」。

抓包基线：Claude Code **2.1.293**（npm `@anthropic-ai/claude-code-linux-x64`，Bun 原生二进制），交互式
`cli` 入口，经本地 mitmproxy、使用伪造的 `sk-ant-oat01-` token，请求全部在本地应答，不触达 Anthropic。
另有多轮对话 + 工具循环的抓包，用于确认 `cc_prev_req`、两个 id 与 `cc_prompt_index` / `cc_turn_index` 的跨轮行为。

## 适用范围

- 覆盖：发往 `CLAUDE_API_BASEURL` 的全部 OAuth 调用——`/v1/messages`、`/v1/models` 代理、
  `/v1/models` 模型发现、限流探测
- 不覆盖：Claude.ai Web 链路；OAuth token 端点（`OAUTH_TOKEN_URL`，仍走 plain httpx）

## 请求行与 HTTP

- `POST https://api.anthropic.com/v1/messages?beta=true`，**HTTP/1.1**（ALPN 只报 `http/1.1`）
- body 为紧凑 JSON（`separators=(",", ":")`，非 ASCII 原样输出）

## Header 顺序与取值

顺序和大小写与 CLI 完全一致（`HEADER_ORDER`）。`Host` 和 `Content-Length` 由传输层计算并放在各自位置。

| # | Header | 值 |
|---|--------|----|
| 1 | `Accept` | `application/json` |
| 2 | `Authorization` | `Bearer <access token>` |
| 3 | `Content-Type` | `application/json` |
| 4 | `User-Agent` | `claude-cli/<version> (external, cli)` |
| 5 | `X-Claude-Code-Session-Id` | 会话 id（= `metadata.user_id.session_id`） |
| 6 | `X-Stainless-Arch` | `x64` |
| 7 | `X-Stainless-Lang` | `js` |
| 8 | `X-Stainless-OS` | `Linux` |
| 9 | `X-Stainless-Package-Version` | `0.128.0` |
| 10 | `X-Stainless-Retry-Count` | `0`；传输层重试的那一次为 `1` |
| 11 | `X-Stainless-Runtime` | `node` |
| 12 | `X-Stainless-Runtime-Version` | `v26.3.0` |
| 13 | `X-Stainless-Timeout` | `600` |
| 14 | `anthropic-beta` | `claude-code-20250219,oauth-2025-04-20` + 客户端 beta（去重、保序） |
| 15 | `anthropic-dangerous-direct-browser-access` | `true` |
| 16 | `anthropic-version` | `2023-06-01` |
| 17 | `x-app` | `cli` |
| 18 | `x-claude-code-prompt-id` | prompt id（= billing 里的 `cc_prompt_id`） |
| 19 | `x-claude-code-request-class` | `main` |
| 20 | `x-client-request-id` | 每个请求新生成的 UUIDv4 |
| 21 | `Connection` | `keep-alive` |
| 22 | `Host` | `api.anthropic.com` |
| 23 | `Accept-Encoding` | `gzip, deflate, br, zstd` |
| 24 | `Content-Length` | body 字节数 |

`X-Stainless-OS` / `X-Stainless-Arch` 固定为抓包平台 `Linux` / `x64`：TLS 指纹是在 linux-x64 上抓的，
macOS 版是否同样发 MLKEM 未经验证，所以不伪装成 macOS。

`/v1/models`（路由代理与模型发现）用同一套 header，去掉 `Content-Type`、`X-Claude-Code-Session-Id`、
`x-claude-code-prompt-id`、`x-claude-code-request-class`。这组 header 是推断的，没有抓到 CLI 自己的
`/v1/models` 请求。`anthropic-version` 固定为 `2023-06-01`，客户端的 `anthropic-version` /
`anthropic-request-id` **不再透传**。

## TLS ClientHello

| 字段 | 值 |
|------|----|
| 密码套件（17 个，按序） | `TLS_AES_128_GCM_SHA256`, `TLS_AES_256_GCM_SHA384`, `TLS_CHACHA20_POLY1305_SHA256`, `ECDHE-ECDSA-AES128-GCM-SHA256`, `ECDHE-RSA-AES128-GCM-SHA256`, `ECDHE-ECDSA-AES256-GCM-SHA384`, `ECDHE-RSA-AES256-GCM-SHA384`, `ECDHE-ECDSA-CHACHA20-POLY1305`, `ECDHE-RSA-CHACHA20-POLY1305`, `ECDHE-ECDSA-AES128-SHA`, `ECDHE-RSA-AES128-SHA`, `ECDHE-ECDSA-AES256-SHA`, `ECDHE-RSA-AES256-SHA`, `AES128-GCM-SHA256`, `AES256-GCM-SHA384`, `AES128-SHA`, `AES256-SHA` |
| 压缩方法 | 仅 `null`（`0100`） |
| 扩展顺序 | `0` server_name, `23` extended_master_secret, `65281` renegotiation_info, `10` supported_groups, `11` ec_point_formats, `35` session_ticket, `16` ALPN, `5` status_request, `13` signature_algorithms, `18` signed_certificate_timestamp, `51` key_share, `45` psk_key_exchange_modes, `43` supported_versions |
| 条件扩展 | `21` padding、`41` pre_shared_key 视 hello 长度 / 会话恢复出现，不参与比对 |
| GREASE | 无（套件、扩展、group 都没有） |
| supported_groups | `X25519MLKEM768`, `X25519`, `P-256`, `P-384` |
| key_share | `X25519MLKEM768` + `X25519` |
| signature_algorithms | `ecdsa_secp256r1_sha256`, `rsa_pss_rsae_sha256`, `rsa_pkcs1_sha256`, `ecdsa_secp384r1_sha384`, `rsa_pss_rsae_sha384`, `rsa_pkcs1_sha384`, `rsa_pss_rsae_sha512`, `rsa_pkcs1_sha512`, `rsa_pkcs1_sha1` |
| supported_versions | TLS 1.3, TLS 1.2 |
| ec_point_formats | uncompressed |
| psk_key_exchange_modes | `psk_dhe_ke` |
| ALPN | `http/1.1` |

Clove 用 rnet（不开浏览器仿真）复现上表，配置经抓包验证与 fixture 零差异：`cipher_list` 按上表顺序并
`preserve_tls13_cipher_list=True`；`curves_list` / `sigalgs_list` 同上表；`grease_enabled=False`、
`permute_extensions=False`、`enable_ech_grease=False`；`session_ticket`、`psk_dhe_ke`、
`enable_ocsp_stapling`、`enable_signed_cert_timestamps`、`renegotiation` 开；`pre_shared_key=False`；
TLS 1.2–1.3；`key_shares_limit=2`；`http1_only`。扩展顺序就是 BoringSSL 默认顺序，rnet 没有也不需要
对应开关。

传输层要点：

- 每个代理 URL 缓存一个 rnet `Client`（LRU 64），连接与 TLS 会话票据按代理复用、不跨代理
- 只用系统信任根；超时按请求传入，未指定时在调用时读取 `REQUEST_TIMEOUT`
- 连接被关闭或重置（rnet `RequestError` / `ConnectionResetError`）时在新连接上重试一次，重试那次带
  `X-Stainless-Retry-Count: 1`。rnet 分不清失效的复用连接和请求发出后才断开的新连接，所以非幂等的
  POST 可能被上游收到两次；CLI 用的 Stainless SDK 也是这样重试的
- 重试后仍失败、连接被拒或 TLS 失败时把代理标记为不健康并返回 `ProxyConnectionError`；超时不标记
  （`REQUEST_TIMEOUT` 是整个请求的总时长，慢生成不代表代理坏了）
- 依赖 `rnet` extra：没有它时 `create_oauth_session` 抛 `RuntimeError`，不回退到其他 HTTP 客户端，
  分到 OAuth 账户的请求直接报错；Web 链路不依赖 rnet，没有可用 OAuth 账户时照常回退过去。Docker 镜像
  始终装有 rnet
- `orig_headers` 传**普通 `list[str]`**（`OrigHeaderMap(init=...)` 会把名字转小写）；`Connection` 与
  `Accept-Encoding` 必须显式设置；`default_headers=False`（它也会屏蔽 `Client(headers=...)`，所以
  header 按请求传）；gzip / br / zstd / deflate 解码开启，解码后的响应 header 不再带
  `Content-Encoding` / `Content-Length`

## Body

### system

`system` 为 `[billing, identity, ...客户端块]`，Clove 插入的两块都不带 `cache_control`（与 CLI 一致）：

- **billing 块**：`x-anthropic-billing-header: cc_version=<version>.<suffix>; cc_entrypoint=cli; cch=<cch>; cc_prev_req=<request-id>; cc_prompt_id=<prompt id>; cc_turn_origin=human; cc_prompt_index=<n>; cc_turn_index=<n>;`
  - 字段顺序固定；`cc_prev_req` 仅在同一会话的第二个及以后的 main 请求出现，位于 `cch` 与
    `cc_prompt_id` 之间，值为**上一个 main 响应**的 `request-id` 响应头（Clove 代理的请求一律以 main 发出；限流探测不计）
  - `cc_turn_index` 是 CLI 的轮次序号（每次提交一条输入算一轮，不论由谁发起），`cc_prompt_index` 是其中
    `cc_turn_origin=human` 的轮次序号；工具循环的 `tool_result` 续请求沿用所在轮次的两个值。2.1.293 抓包：
    第 1 轮、它的 Read 工具续请求、第 2 轮依次为 `1/1`、`1/1`、`2/2`，无工具的三轮为 `1/1`、`2/2`、`3/3`；
    CLI 源码里新一轮的值为上一轮 `turnIndex + 1`、`promptIndex + (origin == human ? 1 : 0)`
  - Clove 发出的轮次都是 `cc_turn_origin=human`，所以两者都取**人类轮数**（与 prompt id 同一个计数：不含
    `tool_result` 块的 user 消息条数，`role: "system"` 等其它角色的消息不计）；人类轮数为 0 时两个字段都省略，
    与 CLI 无法定位轮次（轮次序号小于 1）时的行为一致。CLI 在会话被压缩后或恢复缺少轮次信息的会话时也会省略
    它们，Clove 看不到这些状态，仍按可见的 messages 计数
  - 客户端自带的 billing 块（以 `x-anthropic-billing-header:` 开头）一律丢弃
- **identity 块**：`You are Claude Code, Anthropic's official CLI for Claude.`
  - 客户端 system 的首个块文本恰为这一行时，保留客户端那块（连同它的 `cache_control`），不再插入我们的
  - `INJECT_CLAUDE_CODE_SYSTEM_PROMPT=false` 只省略 identity 块；billing、`metadata.user_id`、header、
    TLS 始终生效
- 客户端 system 为字符串时包成一个 text 块接在后面；`""` / `[]` 视为缺省

### metadata.user_id

JSON **字符串**，紧凑格式，键顺序 `device_id` → `account_uuid` → `session_id`：

- `device_id`：每账户一次性生成的 64 位 hex（`secrets.token_hex(32)`），随账户持久化
- `account_uuid`：OAuth token 响应里的 `account.uuid`，存在 `OAuthToken` 上；粘贴的 token 为 `""`，
  refresh 响应不带时沿用原值。环境变量 token 场景下 CLI 本身也发 `""`
- `session_id`：见下文

### 会话 id 与 prompt id

抓包确认：同一会话内 session id 不变；每个人类轮次换一个新 prompt id，工具循环中的 `tool_result`
续请求沿用原 prompt id；CLI 的 id 都是随机 UUIDv4。

Clove 无状态地派生这两个 id，并保持 UUIDv4 形态（sha256 → 16 字节，强制 version 4 与 RFC 4122 variant 位）：

- session id：`device_id` + 规范化的 `messages[0]`（递归去掉 `cache_control`，`sort_keys`，紧凑格式）
- prompt id：session id + 人类轮数（**不含任何 `tool_result` 块**的 user 消息才算新一轮）
- 客户端提供的合法值优先采用：JSON `metadata.user_id` 里的 v4 `session_id`、v4
  `x-claude-code-prompt-id`
- 客户端的 `x-claude-code-request-class` 不采用，一律发 `main`：采用 `auxiliary` 会得到「auxiliary 类 +
  main 的完整 billing 块与 prompt id」的半成品形状，CLI 从不这样发（它真正的 auxiliary 请求见「刻意不复制的部分」）
- 裁剪或压缩旧消息的客户端会让 id 变化，这是已知取舍
- `cc_prev_req` 无法无状态得到：用进程内有界 map（实际发出的 session id → 上一个 main 响应的
  `request-id`，采用了客户端的 session id 时键就是它；最多 4096 条，不持久化），重启后每个会话的第一个
  请求没有该字段

### cc_version 后缀

`suffix = sha256("59cf53e54c78" + t[4] + t[7] + t[20] + version).hexdigest()[:3]`。`t` 是第一条 user
消息的文本：字符串内容，或第一个非系统提醒的 text 块（仅当块以 `<system-reminder>` 开头**且**包含
`</system-reminder>` 时跳过）。下标按 UTF-16 code unit 计，越界取 `"0"`，孤立代理项按 U+FFFD。
例：`("Say hi", "2.1.283") -> "a03"`，`("Say hi", "2.1.293") -> "2a8"`（均与抓包一致）。

### cch

对「`cch=00000` 时的序列化 body」做规范化后求 xxHash64（seed `0x4D659218E32A3268`），取 `& 0xFFFFF`
并格式化为 5 位小写 hex，再回填到 `cch=` 处。规范化在序列化后的字节上扫描完成：任意深度的
`"model"` 字符串值置为 `""`，删除 `max_tokens` / `fallbacks` / `fallback_credit_token` 键，并保留 CLI
的一个怪癖——对象末尾连续删除两个及以上键时，前面的逗号会留下（得到 `{"keep":1,}` 这样的非法
JSON）。所以 cch 只能在字节层面计算，不能对 dict 规范化后重新序列化。实现与测试向量移植自
CLIProxyAPI（见文末）。

### 序列化与键顺序

- 未知字段原样保留；客户端已有的键保持客户端顺序；只有 Clove 新插入的键按 CLI 位置放：`system`
  放在 `messages` 之后，`metadata` 放在 `tools` 之后（无 `tools` 时放在 `system` 之后）
- 字符串与 `JSON.stringify` 一致（孤立代理项输出 `\udXXX` 转义）；`NaN` / `Infinity` 拒绝并返回 400；
  浮点数按 Python 规则输出（`1e5` 变成 `100000.0`），上游接受
- 发出的字节 == 被签名的字节；输入 body 从不被原地修改

## 版本

- `cli_version()` 默认是 `FALLBACK_CLI_VERSION`，即抓包版本（当前 `2.1.293`，与 fixture 的 `_cli_version`
  一致）。运行时不跟随 npm：新版 CLI 可能改了线上格式，只换版本号会发出「新版本号 + 旧版本布局」这种真实
  CLI 不会发送的组合
- Anthropic 按模型设置 CLI 版本门槛（400 `Claude Code X does not support this model; version Y or newer is required`）：
  收到时升到 Y（只升不降，仅在本进程内有效）并重试一次。这是安全网，不是升级途径
- `CLAUDE_CODE_VERSION` 覆盖默认版本，并关闭门禁自动升级（门禁 400 直接返回给客户端）。只能用环境变量设置：
  管理页每次保存设置都会重写 `config.json` 并丢掉未知键，写在 `config.json` 里的值下次保存后就没了
- 升级途径：每日漂移检测（`.github/workflows/claude-code-drift.yml`，抓 npm latest 与 fixture 比对）报出 drift
  后，维护者用 `scripts/capture_claude_code.py --write` 重新抓包，按新指纹更新
  `app/utils/claude_code_identity.py` 的常量与 fixture，再跑测试。`--write` 只抓第一个 main 请求；新增或
  改变语义的 billing 字段要再做一次多轮 + 工具循环的抓包来确认

## 限流探测

CLI 自己的额度探测：haiku（`claude-haiku-4-5-20251001`）、`max_tokens: 1`、**没有 `system`、没有 billing 块**、
没有 prompt id header，`x-claude-code-request-class: auxiliary`，自带一组 beta（2.1.293 与 2.1.283 完全相同）。
Clove 的 `_probe_rate_limit` 按同样形状发送，经同一传输层。

## 刻意不复制的部分

以下只在有证据表明上游会校验时再补：

- CLI 的完整 beta 列表（2.1.293 发 18 个，且按模型变化）：Clove 只发两个基础 beta 加客户端自带的
- CLI 自己加的 body 字段：`context_management`、`diagnostics`（含 `previous_message_id`）、
  `output_config`、`safeguards` 等；客户端自带的照原样转发
- 工具续请求上的 `x-claude-code-prev-tool-durations` header
- CLI 的旁路流量：settings / policy_limits / bootstrap / eval / MCP registry / event_logging 等请求，
  以及 auxiliary 类 `/v1/messages`（如标题生成，其 billing 块只有 `cc_version` / `cc_entrypoint` / `cch`）
- CLI 只在特定场景才加的 billing 字段：`cc_workload`、`cc_is_subagent=true`（子 agent 请求）
- 其它入口形态（`sdk-cli`、VS Code 等），始终是 `cc_entrypoint=cli`
- macOS 平台 header、token 端点指纹
- 工具名 MCP 别名、敏感词混淆、删除 `temperature` / `top_p`、把客户端 system 挪进 messages
- CLI 在自己后续 system 块上加的 `cache_control`（Clove 不改客户端块的缓存标记）

## 漂移检测如何抓 Clove 自己的请求

`capture_claude_code.py --clove` 不覆盖任何证书信任：它用 Clove 的 OAuth 传输层（默认信任根）经同一个
mitm 代理发两次请求。第 1 次打 `https://api.anthropic.com/...`，mitm 在证书出现之前就记下了
ClientHello，随后客户端拒绝 mitm 证书、请求失败（如果它**没有**失败，说明传输层不校验证书，
按 drift 退出）；第 2 次以相同 header 与 body 打 `http://api.anthropic.com/...`，由 addon 在本地应答，
不连上游。TLS 取自第 1 次，HTTP 版本、header 与 body 取自第 2 次。

## 致谢

cch 扫描器、测试向量与 billing 文本布局移植自 CLIProxyAPI（MIT，
https://github.com/router-for-me/CLIProxyAPI）：`internal/runtime/executor/claude_signing.go`、
`claude_executor_cloaking.go`。
